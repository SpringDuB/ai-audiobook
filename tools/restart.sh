#!/usr/bin/env bash
# tools/restart.sh — 一键重启后端（serve：接口 + 浏览器界面）与 worker（Linux；用 ss / ps 取端口与进程）。
#
# 与 tools/restart.ps1 行为对齐，做四件事（幂等，跑几遍都一样）：
#   1. 停掉旧的 serve / worker 整条进程链（uv → aiab → python）
#   2. 轮转日志 data/logs/{serve,worker}.{log,err.log} → *.before-restart-<时间戳>
#   3. setsid 后台重启两个进程（关掉终端不受影响），stdout/stderr 接回 data/logs/
#   4. 自检：serve 的 /api/session 能通、worker 进程在，打印 PID 与入口地址
#
# 用法：
#   tools/restart.sh                 # 停旧 + 轮转日志 + 后台重启 + 自检
#   tools/restart.sh --with-tts      # 连 TTS 服务一起拉（吃显存，加载要一会儿）
#   tools/restart.sh --stop          # 只停不启
#   tools/restart.sh --port 8301     # 换后端端口
#   tools/restart.sh --timeout 60    # 自检等待秒数（默认 30）
#
# 默认不动 TTS（它由设置页「一键启动」管理，吃显存，未必每次都想要）。

set -uo pipefail

PORT=8300
TIMEOUT=30
WITH_TTS=0
STOP_ONLY=0

usage() {
  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --port)    PORT="${2:?--port 需要一个端口}"; shift 2 ;;
    --timeout) TIMEOUT="${2:?--timeout 需要秒数}"; shift 2 ;;
    --with-tts) WITH_TTS=1; shift ;;
    --stop)     STOP_ONLY=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *) echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
  esac
done

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo"
log_dir="$repo/data/logs"
mkdir -p "$log_dir"

echo "重启 AI 有声书：serve(端口 $PORT) + worker"

if command -v uv >/dev/null 2>&1; then
  launcher=(uv run aiab)
elif [ -x "$repo/.venv/bin/aiab" ]; then
  launcher=("$repo/.venv/bin/aiab")
else
  echo "找不到 uv，也没有 $repo/.venv/bin/aiab：先跑 uv sync" >&2
  exit 1
fi

# 子进程环境：PYTHONUNBUFFERED 让日志立刻落盘。
# ALL_PROXY/all_proxy 的 scheme 不是 httpx 认得的（比如 socks://，要写 socks5://）时去掉：
# 留着它 httpx 建客户端会直接抛 ValueError，worker 起不来（http(s)_proxy 不受影响）。
child_env=(env)
proxy_value="${ALL_PROXY:-${all_proxy:-}}"
if [ -n "$proxy_value" ]; then
  case "$proxy_value" in
    http://*|https://*|socks5://*|socks5h://*) ;;  # httpx 认得的，保留
    *)
      echo "  · ALL_PROXY=$proxy_value 的 scheme httpx 认不得，子进程里去掉（http(s)_proxy 保留）"
      child_env+=(-u ALL_PROXY -u all_proxy)
      ;;
  esac
fi
child_env+=(PYTHONUNBUFFERED=1)

stack_pids() {
  # serve / worker 的整条进程链：命令行里既带 aiab|audiobook.cli，又带 serve|worker 子命令的才认
  { pgrep -f "(aiab|audiobook\.cli) (serve|worker)( |\$)" 2>/dev/null || true; } \
    | sort -u | grep -Ev "^($$|$PPID)$" || true
}

port_pid() {
  command -v ss >/dev/null 2>&1 || return 0
  ss -ltnpH "sport = :$PORT" 2>/dev/null | sed -n 's/.*pid=\([0-9]\+\).*/\1/p' | head -1
}

pid_of_kind() {  # 该子命令最内层的进程：同一条链里启动最晚的那只（uv → python）
  local kind=$1 pid cands=""
  for pid in $(stack_pids); do
    ps -o args= -p "$pid" 2>/dev/null | grep -qE "(aiab|audiobook\.cli) $kind( |\$)" || continue
    cands="$cands $pid"
  done
  [ -n "$cands" ] || return 0
  ps -o pid= --sort=start_time -p $cands 2>/dev/null | tail -1 | tr -d ' '
}

stop_stack() {
  local pids pid i owner
  pids=$(stack_pids)
  if [ -z "$pids" ]; then
    echo "  · 没有在跑的 serve / worker"
  else
    for pid in $pids; do
      local cmd
      cmd=$(ps -o args= -p "$pid" 2>/dev/null | cut -c1-96)
      echo "  · 停止 PID $pid  ${cmd:-（已在退出）}"
      kill -TERM "$pid" 2>/dev/null || true
    done
  fi
  # 等端口释放（uvicorn 收尾要一两秒）
  for i in $(seq 1 50); do
    [ -z "$(port_pid)" ] && break
    sleep 0.3
  done
  pids=$(stack_pids)
  [ -n "$pids" ] && kill -KILL $pids 2>/dev/null
  # 端口占用者兜底：命令行走不匹配（比如手工 uvicorn）也收掉，只要它确实是 aiab
  owner=$(port_pid)
  if [ -n "$owner" ]; then
    if ps -o args= -p "$owner" 2>/dev/null | grep -qE "aiab|audiobook"; then
      echo "  · 端口 $PORT 仍被 PID $owner 占用，再补一刀"
      kill -KILL "$owner" 2>/dev/null || true
      sleep 0.5
    else
      echo "  ! 端口 $PORT 被别的进程占用：PID $owner $(ps -o args= -p "$owner" 2>/dev/null | cut -c1-80)"
    fi
  fi
}

rotate_logs() {
  local stamp=$1 name
  for name in serve.log serve.err.log worker.log worker.err.log; do
    [ -f "$log_dir/$name" ] || continue
    mv "$log_dir/$name" "$log_dir/$name.before-restart-$stamp" 2>/dev/null \
      || echo "  ! 日志轮转失败（这次先追加）：$name"
  done
}

start_kind() {  # $1 = serve|worker，其余参数透传
  local kind=$1
  shift
  "${child_env[@]}" setsid nohup "${launcher[@]}" "$kind" "$@" \
    >"$log_dir/$kind.log" 2>"$log_dir/$kind.err.log" </dev/null &
  echo $!
}


echo "[1/4] 停掉旧的进程"
stop_stack

if [ "$STOP_ONLY" = 1 ]; then
  echo "已停止（--stop：不重新启动）"
  exit 0
fi

echo "[2/4] 轮转日志"
stamp=$(date +%Y%m%d-%H%M%S)
rotate_logs "$stamp"

echo "[3/4] 后台启动"
serve_launcher=$(start_kind serve --port "$PORT")
worker_launcher=$(start_kind worker)
echo "  · serve  启动器 PID $serve_launcher"
echo "  · worker 启动器 PID $worker_launcher"

if [ "$WITH_TTS" = 1 ]; then
  echo "  · 顺手拉起 TTS 服务（模型加载要一会儿，健康检查稍后自己看设置页）"
  "${launcher[@]}" tts start
fi

echo "[4/4] 自检"
healthy=""
if command -v curl >/dev/null 2>&1; then
  deadline=$((SECONDS + TIMEOUT))
  while [ "$SECONDS" -lt "$deadline" ]; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' -m 3 \
      "http://127.0.0.1:$PORT/api/session" 2>/dev/null || true)
    if [ "$code" = "200" ]; then healthy=1; break; fi
    sleep 0.5
  done
else
  echo "  · 没装 curl，跳过健康自检"
fi

serve_pid=$(port_pid)
[ -n "$serve_pid" ] || serve_pid=$(pid_of_kind serve)
# worker 是 uv 先起壳、再 exec python：给它几秒把子进程挂上，别急着报"没找到"
worker_pid=""
worker_deadline=$((SECONDS + 10))
while [ -z "$worker_pid" ] && [ "$SECONDS" -lt "$worker_deadline" ]; do
  worker_pid=$(pid_of_kind worker)
  [ -n "$worker_pid" ] || sleep 0.3
done

gate_on=0
if [ -n "${AB_UI_PASSWORD:-}" ]; then
  gate_on=1
elif [ -f "$repo/.env" ] && grep -qE '^[[:space:]]*AB_UI_PASSWORD[[:space:]]*=[[:space:]]*[^[:space:]]' "$repo/.env"; then
  gate_on=1
fi

echo ""
if [ "$healthy" = "1" ]; then
  echo "✓ serve   http://127.0.0.1:$PORT/  （PID $serve_pid）"
elif [ -z "$healthy" ]; then
  echo "✗ serve   等了 $TIMEOUT 秒还是没通，看 data/logs/serve.err.log 末尾："
  tail -15 "$log_dir/serve.err.log" 2>/dev/null | sed 's/^/    /'
else
  echo "? serve   跳过自检（PID $serve_pid）"
fi
if [ -n "$worker_pid" ]; then
  echo "✓ worker  PID $worker_pid（最内层进程）"
else
  echo "✗ worker 没找到进程，看 data/logs/worker.err.log"
fi
if [ "$gate_on" = "1" ]; then
  echo "· 入口密码：已开启（打开页面先在 /login 输密码）"
else
  echo "· 入口密码：未开启（打开页面直接进）"
fi
echo "· 日志：data/logs/serve.log · serve.err.log · worker.log · worker.err.log"

if [ "$healthy" = "" ] || [ -z "$worker_pid" ]; then exit 1; fi
exit 0
