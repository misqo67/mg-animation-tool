#!/usr/bin/env python3
"""
MG Tool AI Proxy - 本地代理服务 v6
架构：mg-tool.html → mg-proxy.py(7788) → 写任务队列文件
      → mg-worker.py 轮询任务队列 → catpaw-cli 生成代码 → 写结果文件
      → 代理轮询结果文件 → SSE 回传网页

v6 新增：
- 监听 0.0.0.0（支持 Cloudflare Tunnel 公网访问）
- GET / 直接 serve mg-tool.html（无需单独文件服务器）
"""

import json
import os
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler

PROXY_PORT = 7788
TASK_DIR = "/tmp/mg-tool-tasks"
RESULT_DIR = "/tmp/mg-tool-results"
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mg-tool.html")

os.makedirs(TASK_DIR, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[Proxy] {fmt % args}")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        if self.path == '/health':
            body = json.dumps({
                "status": "ok",
                "catpaw_port": 1,
                "port_status": "ok",
                "proxy_port": PROXY_PORT,
                "mode": "queue-file"
            }).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)

        elif self.path in ('/', '/index.html', '/mg-tool.html'):
            # 直接 serve HTML 文件
            try:
                with open(HTML_FILE, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'mg-tool.html not found')

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != '/generate':
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            req_data = json.loads(body)
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        user_prompt = req_data.get('prompt', '').strip()
        if not user_prompt:
            self.send_response(400)
            self.end_headers()
            return

        # SSE 响应头
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

        def sse(obj):
            try:
                self.wfile.write(
                    f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()
                )
                self.wfile.flush()
            except Exception:
                pass

        # 生成唯一任务 ID
        task_id = str(uuid.uuid4())[:8]
        task_file = os.path.join(TASK_DIR, f"task-{task_id}.json")
        result_file = os.path.join(RESULT_DIR, f"result-{task_id}.json")

        # 写任务队列文件
        with open(task_file, 'w', encoding='utf-8') as f:
            json.dump({
                "task_id": task_id,
                "prompt": user_prompt,
                "result_file": result_file,
                "created_at": time.time()
            }, f, ensure_ascii=False)

        print(f"[Proxy] Task {task_id} written: {task_file}")
        sse({"type": "status", "message": "任务已提交，AI 正在生成动效代码..."})

        # 轮询结果文件（最多等 5 分钟）
        deadline = time.time() + 300
        heartbeat_counter = 0

        while time.time() < deadline:
            if os.path.exists(result_file):
                try:
                    with open(result_file, 'r', encoding='utf-8') as f:
                        content = f.read().strip()
                    if content:
                        try:
                            data = json.loads(content)
                            if data.get('error'):
                                sse({"type": "error", "message": data['error']})
                            else:
                                print(f"[Proxy] Got result for task {task_id}")
                                sse({"type": "result", "data": data})
                                sse({"type": "done"})
                            os.remove(result_file)
                            return
                        except json.JSONDecodeError:
                            time.sleep(0.3)
                            continue
                except Exception as e:
                    print(f"[Proxy] Read error: {e}")

            heartbeat_counter += 1
            if heartbeat_counter % 5 == 0:
                sse({"type": "heartbeat"})

            time.sleep(2)

        # 超时
        sse({"type": "error", "message": "生成超时（5分钟），请重试"})
        for fp in [task_file, result_file]:
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass


def run():
    server = HTTPServer(('0.0.0.0', PROXY_PORT), ProxyHandler)
    print(f"[Proxy] v6 Running on http://0.0.0.0:{PROXY_PORT}")
    print(f"[Proxy] HTML: {HTML_FILE}")
    print(f"[Proxy] Task dir:   {TASK_DIR}")
    print(f"[Proxy] Result dir: {RESULT_DIR}")
    server.serve_forever()


if __name__ == '__main__':
    run()
