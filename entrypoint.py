import os
import sys
import runpy

def run():
    # If launched within Apify Actor environment
    if os.environ.get("APIFY_IS_AT_HOME") or os.environ.get("APIFY_ACTOR_ID") or os.environ.get("APIFY_META_ORIGIN"):
        print("[Pulse402 Entrypoint] Detected Apify Actor environment. Running apify/apify_main.py...")
        runpy.run_path("apify/apify_main.py", run_name="__main__")
    else:
        print("[Pulse402 Entrypoint] Detected Cloud/Render server environment. Starting uvicorn app:app...")
        import uvicorn
        port = int(os.environ.get("PORT", 8080))
        uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)

if __name__ == "__main__":
    run()
