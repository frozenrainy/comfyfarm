"""ComfyFarm frontend extension — install on the UI worker (a stock ComfyUI behind the ComfyFarm gateway).

Adds, through the official frontend extension API only:
  - a "ComfyFarm group" setting; node definitions and submissions follow the chosen worker group
  - retry-safe submit: one prompt_id per click, retried with the same id on 503 / network errors (no duplicates)
"""
WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}
