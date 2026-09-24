// PM2 app "sessions": ONE ttyd for every topic-session of the library.
// The dashboard opens /sess/?arg=<id>; ttyd -a hands the ?arg= value to
// open-session.sh, which validates it, loads the topic into tmux (cs-<id>) and
// attaches the tab. Bound to localhost; nginx (/sess/, cookie auth) fronts it.
// -O: the websocket's Origin must equal its Host. The login cookie is
// SameSite=Lax, so without it any *.reimake.com page could open a shell here.
//   start:  pm2 start sessions.pm2.config.js && pm2 save
const path = require("path");

module.exports = {
  apps: [
    {
      name: "sessions",
      script: "/usr/bin/ttyd",
      interpreter: "none",
      args: ["-W", "-a", "-O", "-i", "lo", "-p", "3031", "--base-path", "/sess",
             "bash", path.join(__dirname, "open-session.sh")],
      cwd: path.dirname(__dirname),
      env: { LANG: "C.UTF-8" },
      autorestart: true,
    },
  ],
};
