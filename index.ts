import 'dotenv/config';
import axios, { AxiosInstance } from 'axios';
import { AgentRunner, ChatMessage } from './agent';

const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const ALLOWED_TELEGRAM_USER_IDS = new Set(
  (process.env.ALLOWED_TELEGRAM_USER_IDS || '')
    .split(',')
    .map((x) => x.trim())
    .filter(Boolean)
);
const POLL_TIMEOUT_SECONDS = Number(process.env.POLL_TIMEOUT_SECONDS || '30');
const MAX_HISTORY_MESSAGES = Number(process.env.MAX_HISTORY_MESSAGES || '20');

if (!TELEGRAM_BOT_TOKEN) {
  throw new Error('Missing TELEGRAM_BOT_TOKEN');
}

const telegram: AxiosInstance = axios.create({
  baseURL: `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}`,
  timeout: 60_000,
});

type TelegramUpdate = {
  update_id: number;
  message?: {
    message_id: number;
    date: number;
    text?: string;
    chat: {
      id: number;
      type: string;
    };
    from?: {
      id: number;
      is_bot: boolean;
      first_name?: string;
      username?: string;
    };
  };
};

const sessions = new Map<number, ChatMessage[]>();

function trimHistory(history: ChatMessage[]): ChatMessage[] {
  if (history.length <= MAX_HISTORY_MESSAGES) return history;
  return history.slice(history.length - MAX_HISTORY_MESSAGES);
}

function splitTelegramMessage(text: string, maxLen: number): string[] {
  if (text.length <= maxLen) return [text];

  const parts: string[] = [];
  let remaining = text;

  while (remaining.length > maxLen) {
    let splitAt = remaining.lastIndexOf('\n', maxLen);
    if (splitAt < maxLen * 0.5) splitAt = maxLen;
    parts.push(remaining.slice(0, splitAt));
    remaining = remaining.slice(splitAt).trimStart();
  }

  if (remaining.length) parts.push(remaining);
  return parts;
}

async function sendMessage(chatId: number, text: string): Promise<void> {
  for (const chunk of splitTelegramMessage(text, 3900)) {
    await telegram.post('/sendMessage', {
      chat_id: chatId,
      text: chunk,
      disable_web_page_preview: true,
    });
  }
}

function getSession(chatId: number): ChatMessage[] {
  return sessions.get(chatId) || [];
}

function setSession(chatId: number, messages: ChatMessage[]): void {
  sessions.set(chatId, trimHistory(messages));
}

function isAllowedUser(userId?: number): boolean {
  if (!userId) return false;
  return ALLOWED_TELEGRAM_USER_IDS.has(String(userId));
}

async function handleCommand(chatId: number, text: string): Promise<boolean> {
  const trimmed = text.trim();

  if (trimmed === '/start') {
    await sendMessage(
      chatId,
      [
        'Bot is running.',
        '',
        'Commands:',
        '/start',
        '/reset',
        '/model',
        '/help',
        '',
        'Examples:',
        'Create a new Astro project called demo in ./sites/demo, start it in dev, and give me the preview link.',
        'Show running dev servers.',
      ].join('\n')
    );
    return true;
  }

  if (trimmed === '/help') {
    await sendMessage(
      chatId,
      [
        'This bot can now use local tools.',
        '',
        'Supported patterns:',
        '- create files',
        '- run allowed shell commands',
        '- start background dev servers',
        '- return preview URLs',
        '',
        'Keep requests explicit about folder names and project paths.'
      ].join('\n')
    );
    return true;
  }

  if (trimmed === '/reset') {
    sessions.delete(chatId);
    await sendMessage(chatId, 'Session reset.');
    return true;
  }

  if (trimmed === '/model') {
    await sendMessage(
      chatId,
      `Current model: ${process.env.OPENROUTER_MODEL || 'not set'}`
    );
    return true;
  }

  return false;
}

async function main(): Promise<void> {
  let offset = 0;
  const agent = new AgentRunner();

  console.log('Telegram bot started');

  while (true) {
    try {
      const res = await telegram.get('/getUpdates', {
        params: {
          timeout: POLL_TIMEOUT_SECONDS,
          offset,
        },
      });

      const updates: TelegramUpdate[] = res.data?.result || [];

      for (const update of updates) {
        offset = update.update_id + 1;

        const msg = update.message;
        if (!msg?.text) continue;

        const chatId = msg.chat.id;
        const userId = msg.from?.id;
        const text = msg.text.trim();

        if (!isAllowedUser(userId)) {
          await sendMessage(chatId, 'Not authorised.');
          continue;
        }

        if (await handleCommand(chatId, text)) {
          continue;
        }

        await sendMessage(chatId, 'Working on it...');

        const history = getSession(chatId);

        try {
          const result = await agent.run({
            userMessage: text,
            history,
          });

          setSession(chatId, result.history);
          await sendMessage(chatId, result.output || '(empty response)');
        } catch (error) {
          const message =
            error instanceof Error ? error.message : 'Unknown error';
          await sendMessage(chatId, `Error: ${message}`);
        }
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      console.error('Polling error:', message);
      await new Promise((resolve) => setTimeout(resolve, 3000));
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
