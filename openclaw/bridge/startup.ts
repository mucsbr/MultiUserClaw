import path from "node:path";

export interface GatewayClientLike {
  start(): Promise<void>;
  stop(): void;
}

export interface BridgeStartupConfigLike {
  openclawHome: string;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function connectClientWithRetry(
  client: GatewayClientLike,
  options: {
    maxWaitMs?: number;
    retryIntervalMs?: number;
  } = {},
): Promise<void> {
  const maxWaitMs = options.maxWaitMs ?? 60_000;
  const retryIntervalMs = options.retryIntervalMs ?? 200;
  const start = Date.now();
  let lastError: unknown;

  while (Date.now() - start < maxWaitMs) {
    try {
      await client.start();
      return;
    } catch (error) {
      lastError = error;
      client.stop();
      await sleep(retryIntervalMs);
    }
  }

  throw new Error(
    `Gateway did not become ready within ${maxWaitMs}ms${lastError instanceof Error ? `: ${lastError.message}` : ""}`,
  );
}

export function buildGatewayEnv(
  env: Record<string, string | undefined>,
  config: BridgeStartupConfigLike,
): Record<string, string | undefined> {
  const result: Record<string, string | undefined> = {
    ...env,
    OPENCLAW_CONFIG_PATH: path.join(config.openclawHome, "openclaw.json"),
    OPENCLAW_STATE_DIR: config.openclawHome,
  };
  // BRIDGE_ENABLE_CHANNELS=1 means channels should be active.
  // The Docker image sets OPENCLAW_SKIP_CHANNELS=1 for faster startup,
  // so we explicitly remove it when channels are requested.
  if (env.BRIDGE_ENABLE_CHANNELS === "1") {
    delete result.OPENCLAW_SKIP_CHANNELS;
    delete result.OPENCLAW_SKIP_PROVIDERS;
  }
  return result;
}

export function formatStartupStartedAt(startedAt: Date): string {
  return startedAt.toISOString();
}

export function formatStartupDuration(durationMs: number): string {
  if (durationMs < 1000) {
    return `${durationMs}ms`;
  }
  return `${durationMs}ms (${(durationMs / 1000).toFixed(2)}s)`;
}
