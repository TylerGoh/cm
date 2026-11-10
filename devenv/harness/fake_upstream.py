"""A fake upstream: GET /jobs/<job_id> answers a small zip for any job id, the same bytes every
time, so the file server's sha256 is stable. The zip holds app/version.txt (the job id, which the
fake worker reports after loading it) and app/notes.txt."""
import argparse
import io
import re
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

JOB_ID = re.compile(r"^/jobs/([A-Za-z0-9._-]{1,128})/?$")
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def artifact(job_id: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in (("app/version.txt", f"{job_id}\n"),
                           ("app/notes.txt", f"fake artifact for {job_id}\n")):
            z.writestr(zipfile.ZipInfo(name, FIXED_TIME), text)
    return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def do_GET(self) -> None:
        match = JOB_ID.match(self.path)
        if not match:
            self.send_error(404, "expected /jobs/<job_id>")
            return
        body = artifact(match.group(1))
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
