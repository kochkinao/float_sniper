module.exports = {
  apps: [
    {
      name: "cs2-float-sniper",
      cwd: "/home/hermes/cs2-float-sniper",
      script: "./run_steam_alert_bot.sh",
      interpreter: "bash",
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      time: true,
      out_file: "/home/hermes/cs2-float-sniper/output/pm2-out.log",
      error_file: "/home/hermes/cs2-float-sniper/output/pm2-error.log",
      merge_logs: true
    }
  ]
};
