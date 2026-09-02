# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

# NOTE: Do not eagerly import backup_server here.
#
# Importing backup_server runs its module-level setup_logging() (console ->
# stdout) and configure_web_ui(), which logs "Serving web UI from ..." to
# stdout. Code that only needs stdout to stay clean (e.g. `--output json`
# consumers) should not have to pay for that as a side effect of importing
# something from this package.
#
# The server is loaded explicitly via `python.srv.backup_server:app`
# (uvicorn / `from python.srv.backup_server import app`), so nothing depends on
# it being re-exported from this package. The `abr` CLI no longer imports
# anything from python.srv at all; python.cli.core.logger_conf is its own
# copy of the server's python.srv.core.logger_conf instead of a shared
# source location. (secure_archive.py lives only here, srv-side: the server
# encrypts/decrypts `system export`/`system import` archives itself, the CLI
# just forwards the password and the encrypted bytes over the API.)
