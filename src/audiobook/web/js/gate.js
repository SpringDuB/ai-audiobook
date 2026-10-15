// 入口密码页：提交密码 → POST /api/login → 回首页。
// 密码没开、或这台设备已经登录过：不停在登录页，直接送回 "/"。

const form = document.getElementById("gate-form");
const input = document.getElementById("gate-password");
const button = document.getElementById("gate-submit");
const errorBox = document.getElementById("gate-error");

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
  button.disabled = false;
  input.select();
}

async function readDetail(response) {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") return body.detail;
  } catch (error) {
    /* 非 JSON 的报错就用状态码兜底 */
  }
  return `${response.status} ${response.statusText}`;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const password = input.value;
  if (!password) return;
  button.disabled = true;
  errorBox.hidden = true;
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (response.ok) {
      window.location.replace("/");
      return;
    }
    if (response.status === 401) {
      showError("密码不对，再试一次。");
      return;
    }
    showError(`进不去（${await readDetail(response)}）`);
  } catch (error) {
    showError(`连不上后端：${error?.message || error}`);
  }
});

// 进页面先问一次：没有密码（或已经登录）就没必要让人对着登录框发呆
fetch("/api/session")
  .then((response) => response.json())
  .then((session) => {
    if (!session.auth_required || session.authenticated) window.location.replace("/");
  })
  .catch(() => {
    /* 后端没起来：留在这个页面，提交时会给更具体的报错 */
  });
