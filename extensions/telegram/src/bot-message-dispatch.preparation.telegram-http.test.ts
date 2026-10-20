import { describe, expect, it } from "vitest";
import { createTelegramDispatchHttpFixture } from "./bot-message-dispatch.telegram-http.test-support.js";

describe("Telegram preparation feedback through the real reply pipeline and HTTP", () => {
  const http = createTelegramDispatchHttpFixture();
  const telegramCfg = {
    streaming: { mode: "progress" as const, progress: { commentary: true, toolProgress: true } },
  };

  it("delivers a receipt before resolver history work and keeps one final preview owner", async () => {
    await http.dispatchProgressTurn(async (options) => {
      expect([...http.visibleMessages.values()].join(" ")).toContain("Message received");
      await options?.onReplyPreparation?.("context");
      expect([...http.visibleMessages.values()].join(" ")).toContain("Preparing my reply");
    }, { mode: "progress", toolProgress: true, telegramCfg, finalReply: { text: "Finished." } });
    expect([...http.visibleMessages.values()]).toEqual(["Finished."]);
    expect(http.calls.filter((call) => call.method === "sendMessage")).toHaveLength(1);
  });

  it("lets an all-message reaction replace the receipt while retaining preparation feedback", async () => {
    await http.dispatchProgressTurn(async (options) => {
      expect(http.visibleMessages.size).toBe(0);
      await options?.onReplyPreparation?.("context");
      expect([...http.visibleMessages.values()].join(" ")).toContain("Preparing my reply");
    }, {
      mode: "progress", toolProgress: true, telegramCfg,
      cfg: { messages: { ackReactionScope: "all", ackReaction: "👍" } },
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
