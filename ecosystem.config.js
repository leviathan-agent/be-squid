module.exports = {
  apps: [
    {
      name: "be-squid",
      cwd: __dirname,
      script: "deploy/run.sh",
      interpreter: "bash",
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      // All configuration lives in .env (see .env.squid.example);
      // deploy/run.sh sources it and execs .venv/bin/python ln-agent.py.
      // v0 runs COMMENT_ONLY — the chat-bot process is intentionally absent.
    },
  ]
};
