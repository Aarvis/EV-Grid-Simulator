import argparse
import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Run the local Greensboro Grid Lab dashboard")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    print(f"Greensboro Grid Lab: http://localhost:{args.port}", flush=True)
    uvicorn.run("gridlab.server:app", host="127.0.0.1", port=args.port)

if __name__ == "__main__":
    main()