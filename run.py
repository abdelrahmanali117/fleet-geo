"""
One command to run the demo:

    python run.py              # seed if empty, start simulator + web app, open the browser
    python run.py --reset      # wipe and reseed the fleet
    python run.py --no-sim     # web app only (run `python -m app.simulator` in another terminal)
"""
import argparse
import logging
import os
import sys
import threading
import webbrowser

import uvicorn

from app import config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="drop and reseed all demo collections")
    ap.add_argument("--fleet", type=int, help="number of trucks to seed (default from .env)")
    ap.add_argument("--no-sim", action="store_true", help="don't run the simulator inside the web process")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s  %(message)s")
    if not config.MONGODB_URI:
        sys.exit("MONGODB_URI missing. Copy .env.example to .env and paste your Atlas connection string.")

    from app.seed import seed
    try:
        seed(reset=a.reset, fleet_size=a.fleet)
    except Exception as e:
        sys.exit(f"Could not reach Atlas: {e}\n\nCheck the connection string and that your IP is on the Atlas access list.")

    if a.no_sim:
        os.environ["SIMULATE"] = "false"
        config.SIMULATE = False

    url = f"http://{config.HOST}:{config.PORT}"
    if not a.no_browser:
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    print(f"\n  Fleet demo running at {url}\n")
    uvicorn.run("app.api:app", host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
