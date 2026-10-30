"""Every text the guests module shows a person — the guest bot, the owner's invitations and
alerts, the turnstiles' refusals, the rules written into a sandbox's CLAUDE.md, the
owner's command-line output — in one table per language.

GUESTS_LANG in guests/.env picks the table: ``en`` (default) or ``ru``. Read on every
call, so a changed .env applies without a restart. Templates use str.format: a literal
brace is written ``{{`` / ``}}``. Both tables have the same keys and the same fields
(tests/test_texts.py checks it); the Russian table is the module's original wording,
byte for byte (tests/golden_ru.json).
"""

from __future__ import annotations

import string
from typing import Mapping, Optional

import guestenv

DEFAULT = "en"

EN = {
    # -- the guest bot ---------------------------------------------------------------- #
    "stranger": "This is a private bot. The owner gives access.",
    "cmd_web": "browser address and password",
    "cmd_services": "what runs without a terminal",
    "ago_min": "{m} min",
    "ago_h_min": "{h} h {m} min",
    "ago_h": "{h} h",
    "ago_d": "{d} d",
    "svc_refresh": "🔄 Refresh",
    "svc_unavailable": "The list of services is not available right now: the sandbox is "
                       "restarting or still on an old version. Try again in a minute.",
    "svc_none": "No services yet.\n\nA service is a program that runs without an open "
                "terminal: Tailscale, a site's server, a bot. Ask the agent: "
                "\"make this a service\".",
    "svc_header": "Sandbox services ({running} of {total} running):",
    "svc_restarts": ", restarts: {n}",
    "svc_running": "🟢 {name} — running",
    "svc_crashed": "crashed (exit code {code}), restarting soon",
    "svc_starting": "starting",
    "svc_waiting": "🟠 {name} — {why}{tail}",
    "svc_skipped_limit": "⚪ {name} — not started: at most {limit} services allowed",
    "svc_skipped": "⚪ {name} — not started: too many services",
    "svc_stopped": "⚪ {name} — stopped",
    "svc_logs": "Logs: /work/.services/<name>.log",
    "svc_note_restart": "Restarting {svc}",
    "svc_note_stop": "Stopping {svc}",
    "svc_note_start": "Starting {svc}",
    "svc_note_failed": "Didn't work",
    "svc_note_unknown": "Unknown button",
    "web_not_ready": "Your browser address is not ready yet — ask the owner.",
    "web_intro": "Hello! This is your sandbox with Claude.\n\n"
                 "Write your tasks right here. Commands:\n"
                 "/new — a new terminal, /list — your terminals, /use — switch,\n"
                 "/restart — restart a stuck terminal, /esc — interrupt Claude,\n"
                 "/services — what runs without a terminal (Tailscale, a site, a bot),\n"
                 "/web — the address and password again.\n",
    "web_url": "In the browser: {url}",
    "web_password": "Password: {password}",
    "web_change": "After the first login, change it to your own: {url}/change-password",
    "web_kept": "(if you have changed it already, yours is the one that works)",
    "web_sites": "The agent sends you files as internet links; you can publish a site "
                 "as <site>-<your name>.{domain}.",
    "domain_placeholder": "<domain>",
    # -- invitations (the owner decides in the same bot) ------------------------------ #
    "invite_sent": "Your request has been sent to the owner. As soon as they approve it, "
                   "I'll send the address and password here.",
    "invite_waiting": "The owner has your request already — waiting for their answer.",
    "invite_refused": "The owner did not give access.",
    "invite_failed": "Couldn't create the sandbox — the owner already knows and will sort it out.",
    "who_noname": "no name",
    "invite_request": "🔔 Access request: {who}, Telegram ID {uid}.\n"
                      "Let them in? A sandbox \"{name}\" will be created with the default limits.",
    "invite_not_yours": "This is not your button.",
    "invite_decided": "Already decided or in progress.",
    "invite_rejected": "✖ Refused: {who}.",
    "invite_creating": "⏳ Creating a sandbox for {who}…",
    "invite_created": "✅ {who} — sandbox \"{name}\" created, address and password sent.",
    "invite_create_failed": "🚨 Couldn't create sandbox \"{name}\" for {who}: {err}\n"
                            "You can press again.",
    "btn_allow": "✅ Let in",
    "btn_deny": "✖ Refuse",
    # -- the Claude turnstile ----------------------------------------------------------- #
    "gate_bad_pass": "Invalid pass.",
    "gate_disabled": "Guest access is turned off.",
    "gate_path": "This address is closed.",
    "gate_limit_5h": "5-hour limit used up ({spent} of {limit}). It resets at {at}, Los Angeles time.",
    "gate_limit_week": "Weekly limit used up ({spent} of {limit}). It resets on Monday at midnight, "
                       "Los Angeles time.",
    "gate_upstream": "The Claude server is unreachable.",
    "spend_alert": "⚠️ Sandbox {guest}: ≈{hour_pct:.1f}% of the subscription week spent in the "
                   "last hour ({hour_m:.1f}M). Today in total ≈{today_pct:.1f}%.\n"
                   "If this wasn't expected, see what keeps running: the guest's services "
                   "(/services) or `turnstile.py disable {guest}`.",
    "ts_on": "on",
    "ts_off": "OFF",
    "ts_unlimited_until": " NO LIMIT until {until}",
    "ts_list_line": "{name}\t{state}\t5h {five_used} of {five_limit} · week {week_used} of "
                    "{week_limit} (≈{week_pct:.2f}% of the subscription week){lifted}\ttg={tg}",
    "ts_unlimited_set": "{name}: no limit until {until} (Los Angeles)",
    # -- the site gate ---------------------------------------------------------------- #
    "site_bad_name": "site name: latin a-z, digits and hyphens, 1–20 characters",
    "site_bad_port": "port: a number from 1024 to 65535",
    "site_no_domain": "sites are off: the owner has not set a domain (GUESTS_DOMAIN)",
    "site_too_long": "the name is too long",
    "site_guest_off": "the guest is turned off",
    "site_no_ip": "the sandbox has no fixed address — needs guestctl recreate",
    "site_taken": "this address is taken",
    "site_too_many": "at most {max} sites per guest — take an old one down first",
    "site_failed": "couldn't publish: {err}",
    "site_published": "🌐 Guest {guest} published a site https://{host} "
                      "(port {port} in their sandbox). Take down: sitegate.py remove {host}",
    "site_removed": "🌐 Guest {guest} took down the site https://{host}",
    "site_not_yours": "you have no such site",
    "site_need_pass": "a pass is needed: Authorization: Bearer <pass>",
    "site_routes": "available: GET /sites, POST /publish, DELETE /publish/<site>",
    # -- rules written into the sandbox's CLAUDE.md (for the guest's agent) ------------ #
    "rule_files": "## Files for the user — as an internet link\n"
                  "To give the user a file, put it into `/work/public/` and send the link\n"
                  "`{url}/<file name>` as the last line of the message. Names: latin letters, no spaces.\n"
                  "Everything in `/work/public/` is open to anyone who has the link; never put secrets there.\n",
    "rule_sites": "## A site on the internet: <site>-<guest>.{domain}\n"
                  "Run a server in the sandbox on 0.0.0.0:<port> (1024–65535), then:\n"
                  "`curl -s -X POST http://172.31.250.1:8794/publish -H \"Authorization: Bearer $ANTHROPIC_AUTH_TOKEN\" "
                  "-d '{{\"site\":\"test\",\"port\":3000}}'` — the answer gives the address "
                  "https://test-<guest>.{domain}\n"
                  "(DNS and the certificate take ~1 minute). List: `GET /sites`, take down: `DELETE /publish/<site>`.\n"
                  "At most 3 sites. The server's owner is told about every site.\n",
    "rule_services": "## Services: what must run without a terminal\n"
                     "A terminal is unloaded after 2 hours idle, and everything started from it (`&`, nohup,\n"
                     "tmux) dies with it. A program that must run all the time (Tailscale,\n"
                     "a published site's server, a bot) — make it a service:\n"
                     "1. A file `/work/.services/<name>.sh` (name: lower-case latin, digits, `-`, `_`). Inside:\n"
                     "   a command that runs in the foreground, without `&` and without daemon mode,\n"
                     "   e.g. `exec python3 /work/site/server.py`.\n"
                     "2. The keeper starts it within seconds and restarts it after a crash; output:\n"
                     "   `/work/.services/<name>.log`; state: `curl -s localhost:9876/status`.\n"
                     "3. `curl -X POST localhost:9876/stop/<name>` — stop (the file becomes `<name>.sh.off`),\n"
                     "   `/start/<name>` — start, `/restart/<name>` — restart. Delete the file to remove the service.\n"
                     "At most {max} services. When you add, stop or remove a service, tell the person in one\n"
                     "line: which service and why. The person sees their services with /services in Telegram.\n",
    # -- the owner's command line (guestctl) ------------------------------------------ #
    "ctl_web_done": "https://{host}  (password: {pw_file})",
    "ctl_recreate_busy": "{name} has terminals open ({n}): recreating would cut them off. "
                         "Wait, or use --force.",
    "ctl_no_container": "no container",
    "ctl_list_line": "{name}\ttg={tg}\t:{port}\t{state}\t{on}\t5h {five_used} of {five_limit}"
                     " · week {week_used} of {week_limit}",
    "ctl_open": "OPEN",
    "ctl_closed": "closed",
    "ctl_wall_claude": "Claude turnstile",
    "ctl_wall_telegram": "Telegram turnstile",
    "ctl_wall_ssh": "server's ssh",
    "ctl_wall_nginx": "server's nginx",
    "ctl_wall_internet": "internet (example.com:443)",
}

RU = {
    "stranger": "Это закрытый бот. Доступ выдаёт владелец.",
    "cmd_web": "адрес в браузере и пароль",
    "cmd_services": "что работает без терминала",
    "ago_min": "{m} мин",
    "ago_h_min": "{h} ч {m} мин",
    "ago_h": "{h} ч",
    "ago_d": "{d} дн",
    "svc_refresh": "🔄 Обновить",
    "svc_unavailable": "Список служб сейчас недоступен: песочница перезапускается или ещё на "
                       "старой версии. Попробуйте через минуту.",
    "svc_none": "Служб пока нет.\n\nСлужба — программа, которая работает без открытого "
                "терминала: Tailscale, сервер сайта, бот. Попросите агента: "
                "«сделай это службой».",
    "svc_header": "Службы песочницы (работают {running} из {total}):",
    "svc_restarts": ", перезапусков: {n}",
    "svc_running": "🟢 {name} — работает",
    "svc_crashed": "упала (код {code}), скоро перезапуск",
    "svc_starting": "запускается",
    "svc_waiting": "🟠 {name} — {why}{tail}",
    "svc_skipped_limit": "⚪ {name} — не запущена: разрешено не больше {limit} служб",
    "svc_skipped": "⚪ {name} — не запущена: слишком много служб",
    "svc_stopped": "⚪ {name} — остановлена",
    "svc_logs": "Логи: /work/.services/<имя>.log",
    "svc_note_restart": "Перезапускаю {svc}",
    "svc_note_stop": "Останавливаю {svc}",
    "svc_note_start": "Запускаю {svc}",
    "svc_note_failed": "Не вышло",
    "svc_note_unknown": "Неизвестная кнопка",
    "web_not_ready": "Адрес в браузере для вас ещё не готов — спросите владельца.",
    "web_intro": "Здравствуйте! Это ваша песочница с Claude.\n\n"
                 "Пишите задачи прямо сюда. Команды:\n"
                 "/new — новый терминал, /list — ваши терминалы, /use — переключиться,\n"
                 "/restart — перезапустить зависший терминал, /esc — прервать Claude,\n"
                 "/services — что работает без терминала (Tailscale, сайт, бот),\n"
                 "/web — адрес и пароль ещё раз.\n",
    "web_url": "В браузере: {url}",
    "web_password": "Пароль: {password}",
    "web_change": "После первого входа смените его на свой: {url}/change-password",
    "web_kept": "(если вы его уже сменили — действует ваш)",
    "web_sites": "Файлы агент присылает интернет-ссылками; сайт можно опубликовать "
                 "как <сайт>-<ваше имя>.{domain}.",
    "domain_placeholder": "<домен>",
    "invite_sent": "Запрос на доступ отправлен владельцу. Как только он одобрит, "
                   "я пришлю сюда адрес и пароль.",
    "invite_waiting": "Запрос уже у владельца — ждём его ответа.",
    "invite_refused": "Владелец не дал доступ.",
    "invite_failed": "Не получилось создать песочницу — владелец уже знает и разберётся.",
    "who_noname": "без имени",
    "invite_request": "🔔 Запрос на доступ: {who}, Telegram ID {uid}.\n"
                      "Пустить? Будет создана песочница «{name}» с лимитами по умолчанию.",
    "invite_not_yours": "Это не ваша кнопка.",
    "invite_decided": "Уже решено или в работе.",
    "invite_rejected": "✖ Отказано: {who}.",
    "invite_creating": "⏳ Создаю песочницу для {who}…",
    "invite_created": "✅ {who} — песочница «{name}» создана, адрес и пароль отправлены.",
    "invite_create_failed": "🚨 Не вышло создать песочницу «{name}» для {who}: {err}\n"
                            "Можно нажать ещё раз.",
    "btn_allow": "✅ Пустить",
    "btn_deny": "✖ Отказать",
    "gate_bad_pass": "Неверный пропуск.",
    "gate_disabled": "Доступ гостя выключен.",
    "gate_path": "Этот адрес закрыт.",
    "gate_limit_5h": "Лимит на 5 часов исчерпан ({spent} из {limit}). Сбросится в {at} по Лос-Анджелесу.",
    "gate_limit_week": "Недельный лимит исчерпан ({spent} из {limit}). "
                       "Сбросится в понедельник в полночь по Лос-Анджелесу.",
    "gate_upstream": "Сервер Claude недоступен.",
    "spend_alert": "⚠️ Песочница {guest}: за последний час потрачено ≈{hour_pct:.1f}% "
                   "недели подписки ({hour_m:.1f} млн). Сегодня всего ≈{today_pct:.1f}%.\n"
                   "Если это не ожидалось — посмотреть, что крутится: службы гостя (/services) "
                   "или `turnstile.py disable {guest}`.",
    "ts_on": "вкл",
    "ts_off": "ВЫКЛ",
    "ts_unlimited_until": " БЕЗ ЛИМИТА до {until}",
    "ts_list_line": "{name}\t{state}\t5 ч {five_used} из {five_limit} · неделя {week_used} из "
                    "{week_limit} (≈{week_pct:.2f}% недели подписки){lifted}\ttg={tg}",
    "ts_unlimited_set": "{name}: без лимита до {until} (Лос-Анджелес)",
    "site_bad_name": "имя сайта: латиница a-z, цифры и дефис, 1–20 знаков",
    "site_bad_port": "порт — число от 1024 до 65535",
    "site_no_domain": "сайты выключены: владелец не задал домен (GUESTS_DOMAIN)",
    "site_too_long": "слишком длинное имя",
    "site_guest_off": "гость выключен",
    "site_no_ip": "у песочницы нет постоянного адреса — нужен guestctl recreate",
    "site_taken": "этот адрес занят",
    "site_too_many": "не больше {max} сайтов на гостя — сначала снимите старый",
    "site_failed": "не получилось опубликовать: {err}",
    "site_published": "🌐 Гость {guest} опубликовал сайт https://{host} "
                      "(порт {port} в его песочнице). Снять: sitegate.py remove {host}",
    "site_removed": "🌐 Гость {guest} снял сайт https://{host}",
    "site_not_yours": "такого вашего сайта нет",
    "site_need_pass": "нужен пропуск: Authorization: Bearer <пропуск>",
    "site_routes": "есть: GET /sites, POST /publish, DELETE /publish/<site>",
    "rule_files": "## Файлы для пользователя — интернет-ссылкой\n"
                  "Чтобы отдать файл пользователю, положи его в `/work/public/` и пришли ссылку\n"
                  "`{url}/<имя файла>` последней строкой сообщения. Имена — латиницей, без пробелов.\n"
                  "Всё в `/work/public/` доступно по ссылке любому, у кого она есть; секретов туда не клади.\n",
    "rule_sites": "## Сайт в интернет: <сайт>-<гость>.{domain}\n"
                  "Подними сервер в песочнице на 0.0.0.0:<порт> (1024–65535), затем:\n"
                  "`curl -s -X POST http://172.31.250.1:8794/publish -H \"Authorization: Bearer $ANTHROPIC_AUTH_TOKEN\" "
                  "-d '{{\"site\":\"test\",\"port\":3000}}'` — ответ даст адрес "
                  "https://test-<гость>.{domain}\n"
                  "(DNS и сертификат создаются ~1 минуту). Список: `GET /sites`, снять: `DELETE /publish/<сайт>`.\n"
                  "Не больше 3 сайтов. Владелец сервера получает уведомление о каждом сайте.\n",
    "rule_services": "## Службы: то, что должно работать без терминала\n"
                     "Терминал выгружается после 2 часов простоя, и всё запущенное из него (`&`, nohup,\n"
                     "tmux) умирает вместе с ним. Программу, которая должна работать постоянно (Tailscale,\n"
                     "сервер опубликованного сайта, бот), сделай службой:\n"
                     "1. Файл `/work/.services/<имя>.sh` (имя: строчная латиница, цифры, `-`, `_`). Внутри —\n"
                     "   команда, которая работает на переднем плане, без `&` и без режима демона,\n"
                     "   например `exec python3 /work/site/server.py`.\n"
                     "2. Сторож запустит её за несколько секунд, перезапустит после падения; вывод —\n"
                     "   `/work/.services/<имя>.log`; состояние — `curl -s localhost:9876/status`.\n"
                     "3. `curl -X POST localhost:9876/stop/<имя>` — остановить (файл станет `<имя>.sh.off`),\n"
                     "   `/start/<имя>` — запустить, `/restart/<имя>` — перезапустить. Удалить файл — убрать службу.\n"
                     "Не больше {max} служб. Заведя, остановив или удалив службу, скажи об этом человеку одной\n"
                     "строкой: что за служба и зачем. Свои службы человек видит командой /services в Телеграме.\n",
    "ctl_web_done": "https://{host}  (пароль: {pw_file})",
    "ctl_recreate_busy": "у {name} открыты терминалы ({n}): пересоздание их оборвёт. "
                         "Подождите или --force.",
    "ctl_no_container": "нет контейнера",
    "ctl_list_line": "{name}\ttg={tg}\t:{port}\t{state}\t{on}\t5 ч {five_used} из {five_limit}"
                     " · неделя {week_used} из {week_limit}",
    "ctl_open": "ОТКРЫТ",
    "ctl_closed": "закрыт",
    "ctl_wall_claude": "турникет Claude",
    "ctl_wall_telegram": "турникет Телеграм",
    "ctl_wall_ssh": "ssh сервера",
    "ctl_wall_nginx": "nginx сервера",
    "ctl_wall_internet": "интернет (example.com:443)",
}

TEXTS = {"en": EN, "ru": RU}


def lang(settings: Optional[Mapping[str, str]] = None) -> str:
    """GUESTS_LANG (en | ru); anything else is the default, English."""
    raw = (settings if settings is not None else guestenv.load()).get("GUESTS_LANG") or ""
    raw = raw.strip().lower()
    return raw if raw in TEXTS else DEFAULT


def t(key: str, **fields) -> str:
    return TEXTS[lang()][key].format(**fields)


def fields(template: str) -> set:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}
