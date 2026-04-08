import 'dotenv/config';
import axios, { AxiosInstance } from 'axios';
import { promises as fs } from 'node:fs';
import { existsSync } from 'node:fs';
import { spawn } from 'node:child_process';
import path from 'node:path';
import net from 'node:net';

export type ChatMessage = {
  role: 'system' | 'user' | 'assistant';
  content: string;
};

type AgentRunInput = {
  userMessage: string;
  history: ChatMessage[];
};

type AgentRunResult = {
  output: string;
  history: ChatMessage[];
};

type OpenRouterMessage = {
  role: 'system' | 'user' | 'assistant' | 'tool';
  content?: string;
  tool_call_id?: string;
  name?: string;
  tool_calls?: Array<{
    id: string;
    type: 'function';
    function: {
      name: string;
      arguments: string;
    };
  }>;
};

type ToolSpec = {
  type: 'function';
  function: {
    name: string;
    description: string;
    parameters: Record<string, unknown>;
  };
};

type ToolCall = {
  id: string;
  type: 'function';
  function: {
    name: string;
    arguments: string;
  };
};

type DevServerRecord = {
  id: string;
  name: string;
  projectDir: string;
  cwd: string;
  command: string;
  port: number;
  pid: number;
  url: string;
  startedAt: string;
  logFile: string;
};

const APP_ROOT = process.cwd();
const WORKSPACES_ROOT = path.resolve(
    process.env.WORKSPACES_ROOT || path.join(APP_ROOT, 'workspaces')
);
const DEV_STATE_FILE = path.resolve(
    process.env.DEV_STATE_FILE || path.join(APP_ROOT, 'data', 'dev-servers.json')
);
const PREVIEW_BASE_URL = process.env.PREVIEW_BASE_URL || '';
const DEFAULT_DEV_HOST = process.env.DEFAULT_DEV_HOST || '0.0.0.0';
const DEFAULT_ASTRO_PORT = Number(process.env.DEFAULT_ASTRO_PORT || '4321');
const BASH_PATH = process.env.BASH_PATH || '/usr/bin/bash';

async function ensureDir(dir: string): Promise<void> {
  await fs.mkdir(dir, { recursive: true });
}

function resolveInsideWorkspaces(target: string): string {
  const resolved = path.resolve(WORKSPACES_ROOT, target || '.');
  if (
      resolved !== WORKSPACES_ROOT &&
      !resolved.startsWith(`${WORKSPACES_ROOT}${path.sep}`)
  ) {
    throw new Error(`Path escapes WORKSPACES_ROOT: ${target}`);
  }
  return resolved;
}

async function readTextFileSafe(filePath: string): Promise<string> {
  return fs.readFile(filePath, 'utf8');
}

async function writeTextFileSafe(filePath: string, content: string): Promise<void> {
  await ensureDir(path.dirname(filePath));
  await fs.writeFile(filePath, content, 'utf8');
}

async function loadDevServers(): Promise<DevServerRecord[]> {
  try {
    const raw = await fs.readFile(DEV_STATE_FILE, 'utf8');
    return JSON.parse(raw) as DevServerRecord[];
  } catch {
    return [];
  }
}

async function saveDevServers(records: DevServerRecord[]): Promise<void> {
  await ensureDir(path.dirname(DEV_STATE_FILE));
  await fs.writeFile(DEV_STATE_FILE, JSON.stringify(records, null, 2), 'utf8');
}

function buildPreviewUrl(port: number): string {
  if (PREVIEW_BASE_URL) {
    return PREVIEW_BASE_URL.replace(/\/$/, '') + `:${port}`;
  }
  return `http://YOUR_SERVER_IP:${port}`;
}

function parseArgs(input: string): Record<string, unknown> {
  try {
    return JSON.parse(input || '{}');
  } catch {
    throw new Error('Invalid JSON tool arguments');
  }
}

function validateSpawnEnvironment(cwd: string, command: string): void {
  if (!existsSync(BASH_PATH)) {
    throw new Error(`Bash not found at ${BASH_PATH}`);
  }

  if (!existsSync(cwd)) {
    throw new Error(`Working directory does not exist: ${cwd}`);
  }

  if (!command.trim()) {
    throw new Error('Command cannot be empty');
  }
}

async function isPortOpen(host: string, port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = new net.Socket();
    socket.setTimeout(1500);

    socket.once('connect', () => {
      socket.destroy();
      resolve(true);
    });

    socket.once('timeout', () => {
      socket.destroy();
      resolve(false);
    });

    socket.once('error', () => {
      resolve(false);
    });

    socket.connect(port, host);
  });
}

async function waitForPort(host: string, port: number, timeoutMs = 20000): Promise<void> {
  const start = Date.now();

  while (Date.now() - start < timeoutMs) {
    if (await isPortOpen(host, port)) {
      return;
    }
    await new Promise((r) => setTimeout(r, 500));
  }

  throw new Error(`Timed out waiting for ${host}:${port}`);
}

async function runShellCommand(command: string, cwd: string): Promise<string> {
  const trimmed = command.trim();

  const blockedFragments = [
    'rm -rf /',
    'shutdown',
    'reboot',
    'poweroff',
    'mkfs',
    'dd ',
    'sudo ',
    'su ',
    'systemctl ',
    'mount ',
    'umount ',
    'iptables ',
    'ufw ',
    'docker swarm',
    'passwd ',
    'useradd ',
    'usermod ',
    'chown -R /',
    'kill ',
    'pkill ',
    'killall ',
  ];

  if (blockedFragments.some((fragment) => trimmed.includes(fragment))) {
    throw new Error(`Blocked command: ${trimmed}`);
  }

  const allowedStarts = [
    'pwd',
    'ls',
    'find ',
    'cat ',
    'head ',
    'tail ',
    'sed ',
    'grep ',
    'mkdir ',
    'cp ',
    'mv ',
    'touch ',
    'echo ',
    'git ',
    'npm ',
    'npx ',
    'node ',
    'pnpm ',
    'yarn ',
    'astro ',
    'bash -lc ',
  ];

  if (!allowedStarts.some((prefix) => trimmed === prefix.trim() || trimmed.startsWith(prefix))) {
    throw new Error(`Command not allowed: ${trimmed}`);
  }

  await ensureDir(cwd);
  validateSpawnEnvironment(cwd, trimmed);

  return new Promise((resolve, reject) => {
    const child = spawn(BASH_PATH, ['-lc', trimmed], {
      cwd,
      env: {
        ...process.env,
        PATH: process.env.PATH || '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        CI: '1',
      },
    });

    let stdout = '';
    let stderr = '';

    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
      if (stdout.length > 20000) stdout = stdout.slice(-20000);
    });

    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
      if (stderr.length > 20000) stderr = stderr.slice(-20000);
    });

    const timeout = setTimeout(() => {
      child.kill('SIGTERM');
    }, 5 * 60 * 1000);

    child.on('error', (err) => {
      clearTimeout(timeout);
      reject(
          new Error(
              `Spawn failed.\nBASH_PATH=${BASH_PATH}\nCWD=${cwd}\nCOMMAND=${trimmed}\nDETAILS=${err.message}`
          )
      );
    });

    child.on('close', (code) => {
      clearTimeout(timeout);
      const out = [stdout.trim(), stderr.trim()].filter(Boolean).join('\n');

      if (code === 0) {
        resolve(out || '(no output)');
        return;
      }

      reject(
          new Error(
              `Command failed with exit code ${code}\nCWD=${cwd}\nCOMMAND=${trimmed}\n${out}`
          )
      );
    });
  });
}

async function killPid(pid: number): Promise<void> {
  if (!pid || pid <= 0) return;

  try {
    process.kill(pid, 'SIGTERM');
  } catch {
    return;
  }

  await new Promise((r) => setTimeout(r, 1000));

  try {
    process.kill(pid, 0);
    process.kill(pid, 'SIGKILL');
  } catch {
    // already gone
  }
}

async function stopServersByProjectDir(projectDir: string): Promise<DevServerRecord[]> {
  const records = await loadDevServers();
  const matches = records.filter((r) => r.projectDir === projectDir);
  const remaining = records.filter((r) => r.projectDir !== projectDir);

  for (const record of matches) {
    await killPid(record.pid);
  }

  await saveDevServers(remaining);
  return matches;
}

async function stopServersByPort(port: number): Promise<DevServerRecord[]> {
  const records = await loadDevServers();
  const matches = records.filter((r) => r.port === port);
  const remaining = records.filter((r) => r.port !== port);

  for (const record of matches) {
    await killPid(record.pid);
  }

  await saveDevServers(remaining);
  return matches;
}

async function startBackgroundProcess(args: {
  name: string;
  projectDir: string;
  cwd: string;
  command: string;
  port: number;
}): Promise<DevServerRecord> {
  await ensureDir(path.dirname(DEV_STATE_FILE));
  await ensureDir(args.cwd);
  validateSpawnEnvironment(args.cwd, args.command);

  const logFile = path.join(
      path.dirname(DEV_STATE_FILE),
      `${args.name}-${args.port}.log`
  );

  await ensureDir(path.dirname(logFile));
  const logHandle = await fs.open(logFile, 'a');

  const child = spawn(BASH_PATH, ['-lc', args.command], {
    cwd: args.cwd,
    detached: true,
    stdio: ['ignore', logHandle.fd, logHandle.fd],
    env: {
      ...process.env,
      PATH: process.env.PATH || '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
      HOST: DEFAULT_DEV_HOST,
      PORT: String(args.port),
      CI: '1',
    },
  });

  child.unref();
  await logHandle.close();

  const record: DevServerRecord = {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    name: args.name,
    projectDir: args.projectDir,
    cwd: args.cwd,
    command: args.command,
    port: args.port,
    pid: child.pid ?? 0,
    url: buildPreviewUrl(args.port),
    startedAt: new Date().toISOString(),
    logFile,
  };

  const records = await loadDevServers();
  records.push(record);
  await saveDevServers(records);

  return record;
}

const TOOLS: ToolSpec[] = [
  {
    type: 'function',
    function: {
      name: 'list_files',
      description: 'List files and folders inside a workspace-relative path.',
      parameters: {
        type: 'object',
        properties: {
          target: { type: 'string' }
        },
        required: ['target']
      }
    }
  },
  {
    type: 'function',
    function: {
      name: 'read_file',
      description: 'Read a UTF-8 text file inside WORKSPACES_ROOT.',
      parameters: {
        type: 'object',
        properties: {
          file: { type: 'string' }
        },
        required: ['file']
      }
    }
  },
  {
    type: 'function',
    function: {
      name: 'write_file',
      description: 'Write a UTF-8 text file inside WORKSPACES_ROOT.',
      parameters: {
        type: 'object',
        properties: {
          file: { type: 'string' },
          content: { type: 'string' }
        },
        required: ['file', 'content']
      }
    }
  },
  {
    type: 'function',
    function: {
      name: 'run_command',
      description: 'Run an allowlisted shell command inside a workspace-relative directory.',
      parameters: {
        type: 'object',
        properties: {
          cwd: { type: 'string' },
          command: { type: 'string' }
        },
        required: ['cwd', 'command']
      }
    }
  },
  {
    type: 'function',
    function: {
      name: 'start_astro_dev',
      description: 'Start or restart an Astro project in dev mode from an exact project directory.',
      parameters: {
        type: 'object',
        properties: {
          projectDir: { type: 'string', description: 'Project directory relative to WORKSPACES_ROOT, e.g. sites/demo' },
          port: { type: 'number', description: 'Port to run Astro on' }
        },
        required: ['projectDir', 'port']
      }
    }
  },
  {
    type: 'function',
    function: {
      name: 'stop_dev_server',
      description: 'Stop a running dev server by projectDir, id, or port.',
      parameters: {
        type: 'object',
        properties: {
          projectDir: { type: 'string' },
          server: { type: 'string' }
        }
      }
    }
  },
  {
    type: 'function',
    function: {
      name: 'list_dev_servers',
      description: 'List currently registered background dev servers.',
      parameters: {
        type: 'object',
        properties: {}
      }
    }
  }
];

export class AgentRunner {
  private readonly client: AxiosInstance;
  private readonly model: string;
  private readonly httpReferer?: string;
  private readonly xTitle?: string;

  constructor() {
    const apiKey = process.env.OPENROUTER_API_KEY;
    const model = process.env.OPENROUTER_MODEL;

    if (!apiKey) throw new Error('Missing OPENROUTER_API_KEY');
    if (!model) throw new Error('Missing OPENROUTER_MODEL');

    this.model = model;
    this.httpReferer = process.env.OPENROUTER_HTTP_REFERER;
    this.xTitle = process.env.OPENROUTER_X_TITLE;

    this.client = axios.create({
      baseURL: 'https://openrouter.ai/api/v1',
      timeout: 180_000,
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
        ...(this.httpReferer ? { 'HTTP-Referer': this.httpReferer } : {}),
        ...(this.xTitle ? { 'X-Title': this.xTitle } : {}),
      },
    });
  }

  async run(input: AgentRunInput): Promise<AgentRunResult> {
    await ensureDir(WORKSPACES_ROOT);
    await ensureDir(path.dirname(DEV_STATE_FILE));

    const systemPrompt = [
      'You are a coding and DevOps assistant operated through Telegram.',
      `Your writable sandbox root is WORKSPACES_ROOT=${WORKSPACES_ROOT}.`,
      'Never reference files outside WORKSPACES_ROOT.',
      'Use tools when filesystem or shell access is needed.',
      'For Astro projects, always use start_astro_dev instead of generic background server logic.',
      'When asked to restart or rerun a project, stop the old server for that same project first.',
      'When asked to start an Astro project, treat projectDir as the identity of that running app.',
      `For Astro dev, use "npm run dev -- --host ${DEFAULT_DEV_HOST} --port <port>".`,
      'After starting a dev server, report the returned preview URL clearly.',
      'Be concise and practical.',
      'Do not attempt dangerous system administration.',
    ].join(' ');

    const messages: OpenRouterMessage[] = [
      { role: 'system', content: systemPrompt },
      ...input.history.map((m) => ({
        role: m.role,
        content: m.content,
      })),
      { role: 'user', content: input.userMessage },
    ];

    for (let i = 0; i < 8; i += 1) {
      const response = await this.client.post('/chat/completions', {
        model: this.model,
        messages,
        tools: TOOLS,
        tool_choice: 'auto',
        temperature: 0.2,
      });

      const message = response.data?.choices?.[0]?.message;

      if (!message) {
        throw new Error('No message returned from model');
      }

      const assistantMessage: OpenRouterMessage = {
        role: 'assistant',
        content: typeof message.content === 'string' ? message.content : '',
        tool_calls: Array.isArray(message.tool_calls) ? message.tool_calls : undefined,
      };

      messages.push(assistantMessage);

      const toolCalls: ToolCall[] = Array.isArray(message.tool_calls)
          ? message.tool_calls
          : [];

      if (toolCalls.length === 0) {
        const output = (assistantMessage.content || '').trim();
        const newHistory: ChatMessage[] = [
          ...input.history,
          { role: 'user', content: input.userMessage },
          { role: 'assistant', content: output || '(empty response)' },
        ];

        return {
          output,
          history: newHistory,
        };
      }

      for (const call of toolCalls) {
        const result = await this.executeTool(call);
        messages.push({
          role: 'tool',
          tool_call_id: call.id,
          content: result,
        });
      }
    }

    throw new Error('Too many tool-call rounds');
  }

  private async executeTool(call: ToolCall): Promise<string> {
    const name = call.function.name;
    const args = parseArgs(call.function.arguments);

    switch (name) {
      case 'list_files': {
        const target = String(args.target || '.');
        const fullPath = resolveInsideWorkspaces(target);
        await ensureDir(fullPath);
        const entries = await fs.readdir(fullPath, { withFileTypes: true });
        return entries.map((entry) => `${entry.isDirectory() ? '[DIR]' : '[FILE]'} ${entry.name}`).join('\n') || '(empty directory)';
      }

      case 'read_file': {
        const file = String(args.file || '');
        const fullPath = resolveInsideWorkspaces(file);
        return await readTextFileSafe(fullPath);
      }

      case 'write_file': {
        const file = String(args.file || '');
        const content = String(args.content || '');
        const fullPath = resolveInsideWorkspaces(file);
        await writeTextFileSafe(fullPath, content);
        return `Wrote ${file}`;
      }

      case 'run_command': {
        const cwdRel = String(args.cwd || '.');
        const command = String(args.command || '');
        const cwd = resolveInsideWorkspaces(cwdRel);
        return await runShellCommand(command, cwd);
      }

      case 'start_astro_dev': {
        const projectDir = String(args.projectDir || '');
        const port = Number(args.port || DEFAULT_ASTRO_PORT);

        if (!projectDir) {
          throw new Error('projectDir is required');
        }

        const cwd = resolveInsideWorkspaces(projectDir);
        const packageJsonPath = path.join(cwd, 'package.json');
        const packageJsonRaw = await fs.readFile(packageJsonPath, 'utf8');
        const packageJson = JSON.parse(packageJsonRaw);

        if (!packageJson.scripts || !packageJson.scripts.dev) {
          throw new Error(`No dev script found in ${projectDir}/package.json`);
        }

        await stopServersByProjectDir(projectDir);
        await stopServersByPort(port);

        const record = await startBackgroundProcess({
          name: `astro-${path.basename(projectDir)}`,
          projectDir,
          cwd,
          command: `npm run dev -- --host ${DEFAULT_DEV_HOST} --port ${port}`,
          port,
        });

        await waitForPort('127.0.0.1', port, 20000);

        return JSON.stringify({
          ok: true,
          projectDir,
          port,
          url: record.url,
          pid: record.pid,
          cwd: record.cwd,
          command: record.command,
          logFile: record.logFile,
        }, null, 2);
      }

      case 'stop_dev_server': {
        const projectDir = String(args.projectDir || '');
        const server = String(args.server || '');

        if (projectDir) {
          const stopped = await stopServersByProjectDir(projectDir);
          return stopped.length
              ? `Stopped ${stopped.length} server(s) for ${projectDir}`
              : `No running server found for ${projectDir}`;
        }

        if (server) {
          if (/^\d+$/.test(server)) {
            const stopped = await stopServersByPort(Number(server));
            return stopped.length
                ? `Stopped ${stopped.length} server(s) on port ${server}`
                : `No running server found on port ${server}`;
          }

          const records = await loadDevServers();
          const match = records.find((r) => r.id === server);
          if (!match) {
            throw new Error(`No dev server found for ${server}`);
          }
          await stopServersByProjectDir(match.projectDir);
          return `Stopped server ${server}`;
        }

        throw new Error('Provide projectDir or server');
      }

      case 'list_dev_servers': {
        const records = await loadDevServers();
        if (records.length === 0) return 'No dev servers running.';
        return JSON.stringify(records, null, 2);
      }

      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  }
}
