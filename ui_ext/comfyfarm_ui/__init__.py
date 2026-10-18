# SPDX-License-Identifier: GPL-3.0-or-later
"""ComfyFarm frontend extension — install on the UI worker (a stock ComfyUI behind the ComfyFarm gateway).

Adds, through the official frontend extension API only:
  - a "Group: …" button next to Run (action bar); node definitions and submissions follow the chosen worker group,
    remembered per browser (localStorage)
  - retry-safe submit: one prompt_id per click, retried with the same id on 503 / network errors (no duplicates)
  - inside the dashboard's Workflow tab: a same-origin message to the ComfyFarm shell after each submit
"""
WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}
