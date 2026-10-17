# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

from python.srv import backup_server


class WebUiHostingTests(unittest.TestCase):
    def test_configure_web_ui_serves_spa_routes_without_swallowing_api_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            web_root = pathlib.Path(tmpdir)
            index_html = "<html><body>AltaStor UI</body></html>"
            (web_root / "index.html").write_text(index_html, encoding="utf8")
            assets_dir = web_root / "assets"
            assets_dir.mkdir()
            (assets_dir / "app.js").write_text("console.log('ui');\n", encoding="utf8")
            (web_root / "robots.txt").write_text("User-agent: *\n", encoding="utf8")

            app = FastAPI()

            @app.get("/jobs")
            def list_jobs() -> dict[str, list[dict[str, str]]]:
                return {"items": [{"id": "job-1"}]}

            @app.get("/health")
            def health() -> dict[str, str]:
                return {"status": "ok"}

            backup_server.configure_web_ui(app, web_root=web_root)

            with TestClient(app) as client:
                response = client.get("/")
                self.assertEqual(response.status_code, 200)
                self.assertIn("AltaStor UI", response.text)

                root_head = client.head("/")
                self.assertEqual(root_head.status_code, 200)
                self.assertEqual(root_head.headers["content-type"], "text/html; charset=utf-8")

                spa_response = client.get("/jobs", headers={"accept": "text/html"})
                self.assertEqual(spa_response.status_code, 200)
                self.assertEqual(spa_response.text, index_html)

                spa_head_jobs = client.head("/jobs")
                self.assertEqual(spa_head_jobs.status_code, 200)
                self.assertEqual(spa_head_jobs.headers["content-type"], "text/html; charset=utf-8")

                spa_head_backups = client.head("/backups")
                self.assertEqual(spa_head_backups.status_code, 200)
                self.assertEqual(spa_head_backups.headers["content-type"], "text/html; charset=utf-8")

                api_response = client.get("/jobs")
                self.assertEqual(api_response.status_code, 200)
                self.assertEqual(api_response.json(), {"items": [{"id": "job-1"}]})

                asset_response = client.get("/assets/app.js")
                self.assertEqual(asset_response.status_code, 200)
                self.assertIn("console.log('ui')", asset_response.text)

                file_response = client.get("/robots.txt")
                self.assertEqual(file_response.status_code, 200)
                self.assertIn("User-agent: *", file_response.text)

                fallback_response = client.get("/reporting", headers={"accept": "text/html"})
                self.assertEqual(fallback_response.status_code, 200)
                self.assertEqual(fallback_response.text, index_html)

                api_not_found = client.get("/api/v1/missing", headers={"accept": "text/html"})
                self.assertEqual(api_not_found.status_code, 404)

                health_response = client.get("/health")
                self.assertEqual(health_response.status_code, 200)
                self.assertEqual(health_response.json(), {"status": "ok"})

    def test_configure_web_ui_leaves_api_running_when_web_files_are_missing(self) -> None:
        app = FastAPI()

        @app.get("/health")
        def health() -> dict[str, str]:
            return {"status": "ok"}

        backup_server.configure_web_ui(app, web_root=pathlib.Path("/tmp/altastor-missing-web-ui"))

        with TestClient(app) as client:
            health_response = client.get("/health")
            self.assertEqual(health_response.status_code, 200)
            self.assertEqual(health_response.json(), {"status": "ok"})

            missing_index = client.get("/")
            self.assertEqual(missing_index.status_code, 404)


if __name__ == "__main__":
    unittest.main()
