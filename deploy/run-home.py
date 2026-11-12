import argparse
import os
import subprocess
import time
from pathlib import Path

from dotenv import dotenv_values


AGENT_PORT = 18001
LEDGER_PORT = 18002


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the home AI bill services on loopback.")
    parser.add_argument("--existing-env", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="Validate configuration and files.")
    args = parser.parse_args()

    agent_dir = Path(__file__).resolve().parents[1]
    ledger_dir = agent_dir.parent / "ledger"
    agent_python = agent_dir / ".venv" / "bin" / "python"
    ledger_python = ledger_dir / ".venv" / "bin" / "python"
    required = (args.existing_env, agent_python, ledger_python, agent_dir / "frontend" / "dist" / "index.html")
    for path in required:
        if not path.is_file():
            parser.error(f"Missing required file: {path}")

    existing = dotenv_values(args.existing_env)
    memory_uri = existing.get("AGENT_MONGODB_URI")
    ledger_uri = os.environ.get("AI_BILL_LEDGER_MONGO_URI") or memory_uri
    if not memory_uri or not ledger_uri:
        parser.error("The existing .env needs AGENT_MONGODB_URI.")

    common_env = os.environ.copy()
    for secret_name in ('MONGO_URI', 'AI_BILL_LEDGER_MONGO_URI', 'AGENT_MONGODB_URI'):
        common_env.pop(secret_name, None)
    no_proxy = ",".join(filter(None, (common_env.get("NO_PROXY"), common_env.get("no_proxy"), "127.0.0.1", "localhost")))
    common_env.update(NO_PROXY=no_proxy, no_proxy=no_proxy, PYTHONDONTWRITEBYTECODE="1")
    ledger_env = {
        **common_env,
        "MONGO_URI": ledger_uri,
        "MONGO_DB_NAME": "book_data",
    }
    agent_env = {
        **common_env,
        "AGENT_MONGODB_URI": memory_uri,
        "AGENT_MONGODB_DB": existing.get("AGENT_MONGODB_DB") or "book_agent",
        "BOOKKEEPING_API_BASE_URL": f"http://127.0.0.1:{LEDGER_PORT}",
        "LLM_PROVIDER": "openai_compatible",
        "LLM_BASE_URL": "http://127.0.0.1:11434/v1",
        "LLM_MODEL": "qwen3.5:9b",
        "LLM_API_KEY": "",
        "LLM_REASONING_EFFORT": "none",
        "PLANNER_TIMEOUT_SECONDS": "50",
        "LANGSMITH_TRACING": "false",
        "LANGSMITH_TRACING_V2": "false",
    }
    print(f"Agent 127.0.0.1:{AGENT_PORT} | Ledger 127.0.0.1:{LEDGER_PORT} | qwen3.5:9b", flush=True)
    if args.check:
        return 0

    processes = []
    try:
        for python, directory, app, port, env in (
            (ledger_python, ledger_dir, "app.ai_bill_app:app", LEDGER_PORT, ledger_env),
            (agent_python, agent_dir, "app.main:app", AGENT_PORT, agent_env),
        ):
            processes.append(subprocess.Popen(
                [str(python), "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
                cwd=directory,
                env=env,
            ))
        while True:
            for process in processes:
                status = process.poll()
                if status is not None:
                    return status
            time.sleep(0.25)
    except KeyboardInterrupt:
        return 130
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
