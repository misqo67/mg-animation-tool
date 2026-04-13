#!/usr/bin/env python3
"""
MG Tool AI Proxy - 本地代理服务 v7
架构：mg-tool.html → mg-proxy.py(7788) → 写任务队列文件
      → mg-worker.py 轮询任务队列 → catpaw-cli 生成代码 → 写结果文件
      → 前端 GET /result/<id> 轮询结果

v7 变更：
- 改为轮询模式（POST /generate 返回 task_id，GET /result/<id> 查询结果）
- 解决 Cloudflare Tunnel 对 SSE 流式响应的缓冲问题
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

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        if self.path == '/health':
            self.send_json(200, {
                "status": "ok",
                "catpaw_port": 1,
                "port_status": "ok",
                "proxy_port": PROXY_PORT,
                "mode": "poll"
            })

        elif self.path.startswith('/result/'):
            task_id = self.path[len('/result/'):]
            result_file = os.path.join(RESULT_DIR, f"result-{task_id}.json")
            task_file = os.path.join(TASK_DIR, f"task-{task_id}.json")

            if os.path.exists(result_file):
                try:
                    with open(result_file, 'r', encoding='utf-8') as f:
                        content = f.read().strip()
                    if content:
                        data = json.loads(content)
                        os.remove(result_file)
                        if data.get('error'):
                            self.send_json(200, {"status": "error", "message": data['error']})
                        else:
                            self.send_json(200, {"status": "done", "data": data})
                        return
                except Exception as e:
                    self.send_json(200, {"status": "pending"})
                    return
            elif os.path.exists(task_file):
                self.send_json(200, {"status": "pending"})
            else:
                self.send_json(404, {"status": "not_found"})

        elif self.path in ('/', '/index.html', '/mg-tool.html'):
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
            self.send_json(400, {"error": "invalid json"})
            return

        user_prompt = req_data.get('prompt', '').strip()
        if not user_prompt:
            self.send_json(400, {"error": "prompt required"})
            return

        image_base64 = req_data.get('imageBase64', None)

        task_id = str(uuid.uuid4())[:8]
        task_file = os.path.join(TASK_DIR, f"task-{task_id}.json")
        result_file = os.path.join(RESULT_DIR, f"result-{task_id}.json")

        task_data = {
            "task_id": task_id,
            "prompt": user_prompt,
            "result_file": result_file,
            "created_at": time.time()
        }
        if image_base64:
            task_data["imageBase64"] = image_base64

        with open(task_file, 'w', encoding='utf-8') as f:
            json.dump(task_data, f, ensure_ascii=False)

        print(f"[Proxy] Task {task_id} created")
        self.send_json(200, {"task_id": task_id, "status": "queued"})


def run():
    HTTPServer.allow_reuse_address = True
    server = HTTPServer(('0.0.0.0', PROXY_PORT), ProxyHandler)
    print(f"[Proxy] v7 Running on http://0.0.0.0:{PROXY_PORT}")
    print(f"[Proxy] Mode: poll (POST /generate → GET /result/<id>)")
    server.serve_forever()


if __name__ == '__main__':
    run()
