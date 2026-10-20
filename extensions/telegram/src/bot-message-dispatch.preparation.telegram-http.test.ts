import { describe, expect, it } from "vitest";
import { createTelegramDispatchHttpFixture } from "./bot-message-dispatch.telegram-http.test-support.js";

describe("Telegram preparation feedback through the real reply pipeline and HTTP", () => {
  const http = createTelegramDispatchHttpFixture();
  const telegramCfg = {
    streaming: { mode: "progress" as const, progress: { commentary: true, toolProgress: true } },
  };

  it.each([true, false])("shows preparation with tool telemetry enabled=%s", async (toolProgress) => {
    await http.dispatchProgressTurn(async (options) => {
      expect([...http.visibleMessages.values()].join(" ")).toContain("Message received");
      await options?.onReplyPreparation?.("context");
      expect([...http.visibleMessages.values()].join(" ")).toContain("Preparing my reply");
    }, {
      mode: "progress", toolProgress,
      telegramCfg: { streaming: { mode: "progress", progress: { commentary: true, toolProgress } } },
      finalReply: { text: "Finished." },
    });
    expect([...http.visibleMessages.values()]).toEqual(["Finished."]);
    expect(http.calls.filter((call) => call.method === "sendMessage")).toHaveLength(1);
  });

  it.each([true, false])("uses the channel acknowledgment outcome=%s", async (acknowledged) => {
    const context = http.createContext();
    context.ackReactionPromise = Promise.resolve(acknowledged);
    await http.dispatchProgressTurn(async (options) => {
      expect(http.visibleMessages.size).toBe(acknowledged ? 0 : 1);
      await options?.onReplyPreparation?.("context");
      expect([...http.visibleMessages.values()].join(" ")).toContain("Preparing my reply");
    }, {
      mode: "progress", toolProgress: true, telegramCfg, context,
      finalReply: { text: "Finished." },
    });
    expect([...http.visibleMessages.values()]).toEqual(["Finished."]);
  });

  it("keeps commentary opt-outs silent before the final answer", async () => {
    await http.dispatchProgressTurn(async (options) => {
      await options?.onReplyPreparation?.("context");
      expect(http.visibleMessages.size).toBe(0);
    }, { mode: "progress", toolProgress: true, finalReply: { text: "Finished." } });
  });
});
