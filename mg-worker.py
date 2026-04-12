#!/usr/bin/env python3
"""
MG Tool Worker v3 - 稳定版
修复：
- 任务重复处理（用 .lock 文件防重）
- daemon 线程被强杀导致任务丢失（改为非 daemon + join）
- Reply 提取失败（多种匹配方式 + 调试日志）
- 中文乱码（json.loads 正确解码转义）
"""

import json
import os
import re
import subprocess
import tempfile
import time
import threading

TASK_DIR = "/tmp/mg-tool-tasks"
RESULT_DIR = "/tmp/mg-tool-results"
LOCK_DIR = "/tmp/mg-tool-locks"
CATPAW_CLI = "/Applications/CatPaw Desk.app/Contents/Resources/app.asar.unpacked/node_modules/@catpaw/agent-sdk/bin/catpaw-cli"
WORK_DIR = "/tmp/mg-worker-workspace"
POLL_INTERVAL = 2
MODEL_ID = "8"
SSO_CONFIG_PATH = os.path.expanduser("~/.catpaw/sso_config.json")

os.makedirs(TASK_DIR, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)
os.makedirs(LOCK_DIR, exist_ok=True)
os.makedirs(WORK_DIR, exist_ok=True)

# 正在处理的任务集合（内存级防重，跨重启用 lock 文件）
processing = set()
processing_lock = threading.Lock()


def build_catpaw_env() -> dict:
    """构造 catpaw-cli 所需的环境变量，从 sso_config.json 读取鉴权信息"""
    env = os.environ.copy()
    env["HOME"] = os.path.expanduser("~")

    if "CATPAW_CONFIG_CONTENT" in env:
        return env

    try:
        with open(SSO_CONFIG_PATH, "r", encoding="utf-8") as f:
            sso = json.load(f)
        ssoid = sso.get("ssoid", "")
        mis_id = sso.get("misId", "")
        if ssoid:
            config = {
                "cookie": f"1d47d6ff96_ssoid={ssoid}",
                "baseURL": "https://catpaw.sankuai.com",
                "source": "CatPawDesk",
                "enableSubagent": False,
                "enableArtifacts": True,
                "enableAskQuestion": True,
                "dxNoticeEnable": False,
                "misId": mis_id,
                "mcpServers": {},
                "confirmToolList": [],
                "skipConfirmForArtifactDelete": True,
            }
            env["CATPAW_CONFIG_CONTENT"] = json.dumps(config, ensure_ascii=False)
            print(f"[Worker] Loaded ssoid from sso_config.json (misId={mis_id})")
    except Exception as e:
        print(f"[Worker] Warning: failed to load sso_config.json: {e}")

    return env


def build_prompt(user_prompt: str) -> str:
    return (
        f"需求：{user_prompt}\n\n"
        f"生成 MG 动效 JS 代码，规则：\n"
        f"1. 操作 id=preview-canvas 的 div（375×480，背景#0F0F1A），DOM 已就绪可直接操作\n"
        f"2. 原生JS+CSS，不用外部库，动效流畅精美\n"
        f"3. 颜色用 #7C6EFA #FF6B35 #FF4B6E #34D399\n"
        f"4. 禁止用 element.animate() Web Animations API，改用 CSS @keyframes + style.animation 或 requestAnimationFrame\n"
        f"5. 所有 DOM 操作前先确认元素存在（用 getElementById 获取 preview-canvas 后再操作）\n\n"
        f"只输出这一行JSON，不要解释不要代码块：\n"
        f'{{\"title\":\"名称\",\"code\":\"JS代码(换行用\\\\n)\"}}'
    )


def extract_reply(all_output: str, task_id: str) -> str:
    """从 catpaw-cli 输出中提取 AI 回复，多种方式兜底"""

    # 方式1：标准格式 Assistant: <内容> #XMDJ#
    parts = re.findall(r'Assistant:\s*(.*?)\s*#XMDJ#', all_output, re.DOTALL)
    if parts:
        return "\n".join(parts).strip()

    # 方式2：turn-executor 行里的 Assistant 内容（更宽松）
    m = re.search(r'turn-executor.*?Assistant:\s*(.+)', all_output)
    if m:
        raw = m.group(1)
        return raw.split('#XMDJ#')[0].strip()

    # 方式3：直接找完整 JSON 对象 {"title":...,"code":...}
    m = re.search(r'\{"title"\s*:.*?"code"\s*:.*?\}', all_output, re.DOTALL)
    if m:
        return m.group(0)

    # 方式4：找任意包含 "code" 键的 JSON 对象
    m = re.search(r'\{[^{}]{0,200}"code"\s*:\s*"[^"]{10,}[^{}]{0,200}\}', all_output, re.DOTALL)
    if m:
        return m.group(0)

    # 全部失败，保存调试文件
    debug_file = f"/tmp/mg-debug-{task_id}.txt"
    with open(debug_file, "w", encoding="utf-8") as f:
        f.write(all_output[-5000:])
    print(f"[Worker] All extraction methods failed. Debug saved: {debug_file}")
    return ""


def parse_code(full_reply: str):
    """从 AI 回复中解析出 title 和 code，返回 (title, code)"""
    title = "AI 生成动效"
    code = None

    if not full_reply:
        return title, code

    # 1. 直接解析整行 JSON
    try:
        parsed = json.loads(full_reply)
        return parsed.get("title", title), parsed.get("code")
    except Exception:
        pass

    # 2. 找 ```json 块
    m = re.search(r'```json\s*(\{.*?\})\s*```', full_reply, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(1))
            return parsed.get("title", title), parsed.get("code")
        except Exception:
            pass

    # 3. 找 ```js / ```javascript 块
    m = re.search(r'```(?:javascript|js)\s*(.*?)\s*```', full_reply, re.DOTALL)
    if m:
        return title, m.group(1).strip()

    # 4. 找任意代码块
    m = re.search(r'```\w*\s*(.*?)\s*```', full_reply, re.DOTALL)
    if m:
        return title, m.group(1).strip()

    # 5. 宽松匹配 JSON 里的 code 字段
    m = re.search(r'"title"\s*:\s*"([^"]*)"', full_reply)
    if m:
        title = m.group(1)

    m = re.search(r'"code"\s*:\s*"((?:[^"\\]|\\.)*)"', full_reply, re.DOTALL)
    if m:
        try:
            code = json.loads('"' + m.group(1) + '"')
        except Exception:
            code = m.group(1)

    return title, code


def acquire_lock(task_id: str) -> bool:
    """尝试获取任务锁，返回是否成功（防止重启后重复处理）"""
    lock_file = os.path.join(LOCK_DIR, f"{task_id}.lock")
    if os.path.exists(lock_file):
        # 检查 lock 是否超时（超过 10 分钟认为是僵尸锁）
        if time.time() - os.path.getmtime(lock_file) < 600:
            return False
    try:
        with open(lock_file, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return False


def release_lock(task_id: str):
    lock_file = os.path.join(LOCK_DIR, f"{task_id}.lock")
    try:
        os.remove(lock_file)
    except Exception:
        pass


def process_task(task_file: str):
    task_id = os.path.basename(task_file).replace("task-", "").replace(".json", "")

    # 内存级防重
    with processing_lock:
        if task_id in processing:
            return
        processing.add(task_id)

    # 文件级防重（跨重启）
    if not acquire_lock(task_id):
        print(f"[Worker] Task {task_id} already locked, skipping")
        with processing_lock:
            processing.discard(task_id)
        return

    print(f"[Worker] Processing task: {task_id}")

    try:
        with open(task_file, "r", encoding="utf-8") as f:
            task = json.load(f)
    except Exception as e:
        print(f"[Worker] Failed to read task {task_id}: {e}")
        release_lock(task_id)
        with processing_lock:
            processing.discard(task_id)
        return

    user_prompt = task.get("prompt", "").strip()
    result_file = task.get("result_file", f"{RESULT_DIR}/result-{task_id}.json")

    # 删除任务文件，防止重复领取
    try:
        os.remove(task_file)
    except Exception:
        pass

    if not user_prompt:
        release_lock(task_id)
        with processing_lock:
            processing.discard(task_id)
        return

    prompt_content = build_prompt(user_prompt)
    prompt_data = {"content": [{"type": "text", "text": prompt_content}]}

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as pf:
        json.dump(prompt_data, pf, ensure_ascii=False)
        prompt_file = pf.name

    print(f"[Worker] Calling catpaw-cli (model {MODEL_ID}) for task {task_id}...")
    start = time.time()

    try:
        result = subprocess.run(
            [
                CATPAW_CLI, "create",
                "--prompt-file", prompt_file,
                "--directory", WORK_DIR,
                "--disable-todos",
                "-m", MODEL_ID,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            env=build_catpaw_env(),
        )
        elapsed = time.time() - start
        print(f"[Worker] Done in {elapsed:.1f}s, returncode={result.returncode}")

        # 检查 AI 是否直接写了结果文件
        if os.path.exists(result_file):
            print(f"[Worker] Result file written by AI: {result_file}")
            return

        all_output = (result.stdout or "") + (result.stderr or "")
        full_reply = extract_reply(all_output, task_id)
        print(f"[Worker] Reply length: {len(full_reply)} chars")

        title, code = parse_code(full_reply)

        if code:
            with open(result_file, "w", encoding="utf-8") as f:
                json.dump({"title": title, "code": code}, f, ensure_ascii=False)
            print(f"[Worker] Result written for task {task_id}")
        else:
            print(f"[Worker] Failed. Reply preview: {full_reply[:200]}")
            with open(result_file, "w", encoding="utf-8") as f:
                json.dump({"error": "AI 生成失败，请重试"}, f, ensure_ascii=False)

    except subprocess.TimeoutExpired:
        print(f"[Worker] Timeout for task {task_id}")
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump({"error": "生成超时，请重试"}, f, ensure_ascii=False)
    except Exception as e:
        print(f"[Worker] Exception for task {task_id}: {e}")
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump({"error": f"处理失败: {str(e)}"}, f, ensure_ascii=False)
    finally:
        try:
            os.remove(prompt_file)
        except Exception:
            pass
        release_lock(task_id)
        with processing_lock:
            processing.discard(task_id)


def main():
    print(f"[Worker] v3 started. Model={MODEL_ID}, watching {TASK_DIR} every {POLL_INTERVAL}s")
    print(f"[Worker] Results dir: {RESULT_DIR}")
    print(f"[Worker] Lock dir: {LOCK_DIR}")

    active_threads = []

    while True:
        try:
            # 清理已完成的线程
            active_threads = [t for t in active_threads if t.is_alive()]

            files = sorted(
                [
                    os.path.join(TASK_DIR, f)
                    for f in os.listdir(TASK_DIR)
                    if f.startswith("task-") and f.endswith(".json")
                ],
                key=os.path.getmtime,
            )
            for task_file in files:
                # 非 daemon 线程，主线程退出前会等待它完成
                t = threading.Thread(target=process_task, args=(task_file,), daemon=False)
                t.start()
                active_threads.append(t)

        except Exception as e:
            print(f"[Worker] Poll error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
