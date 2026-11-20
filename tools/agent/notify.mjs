#!/usr/bin/env node
// Telegram notifications for the autonomous agent (ADR-0019). No dependencies; Node >= 22.
//
//   node tools/agent/notify.mjs "<message>"   send a message to the owner
//   node tools/agent/notify.mjs link          after the owner sent /start to the bot: store the chat id
//   node tools/agent/notify.mjs --hook        Claude Code Stop/Notification hook (event JSON on stdin)
//
// The bot token is read here from a private host file and never printed; the agent never reads it.
// The chat id is not a secret; it lives in ~/.config/reader-ai/telegram-chat-id.
import { readFileSync, writeFileSync, mkdirSync, chmodSync, existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';

const TOKEN_FILE = process.env.READER_TG_TOKEN_FILE ?? join(homedir(), 'Developer', '.token_tg_bot');
const CHAT_FILE = join(homedir(), '.config', 'reader-ai', 'telegram-chat-id');
const MAX = 3500; // Telegram caps a message at 4096 characters; leave room for the prefix

const token = () => readFileSync(TOKEN_FILE, 'utf8').trim();
const api = async (method, body) => {
  const res = await fetch(`https://api.telegram.org/bot${token()}/${method}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
  const json = await res.json();
  if (!json.ok) throw new Error(`telegram ${method}: ${json.description ?? res.status}`);
  return json.result;
};

const send = async (text) => {
  if (!existsSync(CHAT_FILE)) throw new Error(`no chat linked: send /start to the bot, then run "notify.mjs link"`);
  const chatId = readFileSync(CHAT_FILE, 'utf8').trim();
  await api('sendMessage', { chat_id: chatId, text: `reader-ai · ${text}`.slice(0, MAX), disable_web_page_preview: true });
};

const link = async () => {
  const updates = await api('getUpdates', { timeout: 0 });
  const chat = updates.map((u) => u.message?.chat).filter(Boolean).at(-1);
  if (!chat) throw new Error('no messages yet: send /start to the bot from your Telegram, then run "link" again');
  mkdirSync(dirname(CHAT_FILE), { recursive: true });
  writeFileSync(CHAT_FILE, `${chat.id}\n`);
  chmodSync(CHAT_FILE, 0o600);
  await send('чат привязан, уведомления агента будут приходить сюда');
  return `linked chat ${chat.id}`;
};

// Claude Code hook events (stdin JSON). Messages to the owner are in Russian (AGENTS.md).
const HOOK_MESSAGES = {
  Stop: () => 'агент остановился. Статус задач: docs/progress.yaml',
  Notification: (e) => `агенту нужно внимание: ${e.message ?? 'без текста'}`,
};

const hook = async () => {
  const raw = readFileSync(0, 'utf8');
  const event = raw ? JSON.parse(raw) : {};
  const render = HOOK_MESSAGES[event.hook_event_name];
  return render ? send(render(event)) : undefined;
};

const commands = {
  link,
  '--hook': () => hook().catch(() => undefined), // a hook never blocks the agent
};

const [arg, ...rest] = process.argv.slice(2);
const run = commands[arg] ?? (() => send([arg, ...rest].join(' ')));

if (!arg) {
  process.stderr.write('usage: notify.mjs "<message>" | link | --hook\n');
  process.exit(1);
}
run()
  .then((out) => { if (typeof out === 'string') process.stdout.write(`${out}\n`); })
  .catch((err) => { process.stderr.write(`${err.message}\n`); process.exit(1); });
