// Tests pending final delivery records and deferred message-tool send behavior.
import { expectDefined } from "@openclaw/normalization-core/expect";
import { describe, expect, it } from "vitest";
import { withAssistantReplySource } from "../../agents/embedded-agent-reply-source.js";
import {
  INTERNAL_RUNTIME_CONTEXT_BEGIN,
  INTERNAL_RUNTIME_CONTEXT_END,
} from "../../agents/internal-runtime-context.js";
import { projectAssistantTranscriptText } from "../../config/sessions/transcript-assistant-delivery-read.js";
import { getReplyPayloadMetadata, setReplyPayloadMetadata } from "../reply-payload.js";
import { markInboundContextLabel } from "./inbound-context-marker.js";
import {
  classifyHeartbeatPendingFinalDelivery,
  sanitizePendingFinalDeliveryText,
} from "./pending-final-delivery-state.js";
import {
  buildRecoverablePendingFinalDeliveryText,
  normalizePendingFinalDeliveryPayloads,
  normalizePendingFinalRecoveryPayloads,
  resolvePendingFinalDeliveryCompletion,
} from "./pending-final-delivery.js";
import { applyReplyThreading } from "./reply-payloads-base.js";

describe("resolvePendingFinalDeliveryCompletion", () => {
  it("persists the session writer authority with durable final custody", () => {
    const payload = setReplyPayloadMetadata(
      { text: "settled final" },
      {
        pendingFinalDeliveryCompletion: {
          deliveryId: "delivery-1",
          intentId: "intent-1",
          sessionId: "session-1",
          sessionKey: "agent:main:telegram:direct:1",
          storePath: "/tmp/sessions.json",
        },
        sessionWriterDeliveryAuthority: {
          agentId: "main",
          expectedLifecycleRevision: "revision-a",
          expectedSessionId: "session-1",
          expectedWriterRunId: "run-a",
          sessionKey: "agent:main:telegram:direct:1",
          storePath: "/tmp/sessions.json",
        },
      },
    );

    expect(resolvePendingFinalDeliveryCompletion([payload])).toEqual({
      kind: "pending-final",
      deliveryId: "delivery-1",
      intentId: "intent-1",
      sessionId: "session-1",
      sessionKey: "agent:main:telegram:direct:1",
      storePath: "/tmp/sessions.json",
      sessionWriterDeliveryAuthority: {
        agentId: "main",
        expectedLifecycleRevision: "revision-a",
        expectedSessionId: "session-1",
        expectedWriterRunId: "run-a",
        sessionKey: "agent:main:telegram:direct:1",
        storePath: "/tmp/sessions.json",
      },
    });
  });
});

it.each(["no_r", "NO_R", "NO_REPLY"])(
  "keeps pending ordinary reply %s during heartbeat admission",
  (text) => {
    expect(classifyHeartbeatPendingFinalDelivery(text, 300)).toEqual({
      shouldClear: false,
      replayText: text,
    });
  },
);

describe("sanitizePendingFinalDeliveryText", () => {
  it("preserves indented code in pending final text", () => {
    const text = "    const value = 1;\n    use(value);";
    expect(sanitizePendingFinalDeliveryText(text)).toBe(text);
  });

  it("strips internal metadata from durable pending delivery text", () => {
    const text = [
      "Visible reply",
      INTERNAL_RUNTIME_CONTEXT_BEGIN,
      "internal detail",
      INTERNAL_RUNTIME_CONTEXT_END,
      "",
      markInboundContextLabel("Conversation info:"),
      "```json",
      '{"message_id":"msg-1"}',
      "```",
    ].join("\n");

    expect(sanitizePendingFinalDeliveryText(text)).toBe("Visible reply");
  });

  it.each(["NO_REPLY", '"NO_REPLY"', '{"action":"NO_REPLY"}', "NO_REPLY."])(
    "preserves prepared text without reinterpreting it: %s",
    (text) => expect(sanitizePendingFinalDeliveryText(text)).toBe(text),
  );

  it.each(["NO_REPLYThe user is saying hello", "HEARTBEAT_OK NO_REPLY"])(
    "preserves mixed silent tokens in recovery text: %s",
    (text) => {
      expect(sanitizePendingFinalDeliveryText(text)).toBe(text);
    },
  );

  it("preserves a prepared leading control-looking line", () => {
    expect(sanitizePendingFinalDeliveryText("NO_REPLY\nAnswer.\n    Code.")).toBe(
      "NO_REPLY\nAnswer.\n    Code.",
    );
  });
});

describe("normalizePendingFinalRecoveryPayloads", () => {
  it("keeps media directives in the durable recovery text while sendable delivery parses them", () => {
    const rawPayloads = [{ text: "Rendered chart\nMEDIA:/tmp/chart.png" }];

    const recoveryPayloads = normalizePendingFinalRecoveryPayloads(rawPayloads);
    expect(recoveryPayloads.map((payload) => payload.text)).toEqual([
      "Rendered chart\nMEDIA:/tmp/chart.png",
    ]);

    const deliveryPayloads = normalizePendingFinalDeliveryPayloads(rawPayloads);
    expect(deliveryPayloads.map((payload) => payload.text)).toEqual(["Rendered chart"]);
  });

  it("encodes structured media URLs as durable media directives", () => {
    expect(
      buildRecoverablePendingFinalDeliveryText([
        { text: "Rendered chart", mediaUrl: "/tmp/chart.png" },
      ]),
    ).toBe("Rendered chart\nMEDIA:/tmp/chart.png");
    expect(
      buildRecoverablePendingFinalDeliveryText([
        {
          text: "MEDIA:/tmp/c.png",
          mediaUrls: [" /tmp/a.png ", "/tmp/b.png", "/tmp/a.png", " "],
          mediaUrl: " /tmp/b.png ",
        },
      ]),
    ).toBe("MEDIA:/tmp/a.png\nMEDIA:/tmp/b.png\nMEDIA:/tmp/c.png");
    expect(buildRecoverablePendingFinalDeliveryText([{ text: "Visible", mediaUrl: " " }])).toBe(
      "Visible",
    );
  });

  it("refuses payload shapes the text marker cannot replay without loss", () => {
    expect(
      buildRecoverablePendingFinalDeliveryText([
        { text: "Pick one", interactive: { blocks: [{ type: "buttons", buttons: [] }] } },
      ]),
    ).toBeUndefined();
    expect(
      buildRecoverablePendingFinalDeliveryText([
        { text: "Secret image", mediaUrl: "/tmp/secret.png", sensitiveMedia: true },
      ]),
    ).toBeUndefined();
    expect(
      buildRecoverablePendingFinalDeliveryText([{ text: "[[reply_to_current]] visible final" }]),
    ).toBeUndefined();
  });

  it("separates implicit delivery threading from explicit reply semantics", () => {
    expect(
      buildRecoverablePendingFinalDeliveryText([
        { text: "Visible final", replyToId: "source-message" },
      ]),
    ).toBe("Visible final");

    const explicitReply = { text: "Visible final", replyToId: "source-message" };
    setReplyPayloadMetadata(explicitReply, { replyToIdExplicit: true });
    expect(buildRecoverablePendingFinalDeliveryText([explicitReply])).toBeUndefined();
  });

  it.each(["first", "all"] as const)(
    "keeps a producer-selected implicit source replayable through final threading (%s)",
    (replyToMode) => {
      const payload = withAssistantReplySource({ text: "Replayable answer" }, "101");
      const finals = applyReplyThreading({
        payloads: [payload],
        replyToMode,
        replyToChannel: "telegram",
        currentMessageId: "103",
      });
      expect(finals[0]?.replyToId).toBe("101");
      expect(
        buildRecoverablePendingFinalDeliveryText(normalizePendingFinalRecoveryPayloads(finals)),
      ).toBe("Replayable answer");
    },
  );

  it("retains explicit inline quotes after capturing an implicit source", () => {
    const payload = withAssistantReplySource({ text: "[[reply_to:90]] Explicit answer" }, "101");
    const finals = applyReplyThreading({
      payloads: [payload],
      replyToMode: "all",
      replyToChannel: "telegram",
      currentMessageId: "103",
    });
    expect(finals[0]).toMatchObject({ text: "Explicit answer", replyToId: "90", replyToTag: true });
    expect(
      getReplyPayloadMetadata(expectDefined(finals[0], "prepared final"))?.replyToIdExplicit,
    ).toBe(true);
    expect(buildRecoverablePendingFinalDeliveryText(finals)).toBeUndefined();
  });

  it.each([
    {
      name: "old text-only transcript",
      transcript: '{"role":"assistant","content":[{"type":"text","text":"Stored answer"}]}',
      expectedTarget: "103",
      expectedText: "Stored answer",
    },
    {
      name: "old explicit quote",
      transcript:
        '{"role":"assistant","content":[{"type":"text","text":"Stored answer"}],"openclawDelivery":{"replyToId":"90"}}',
      expectedTarget: "90",
      expectedText: undefined,
    },
    {
      name: "new implicit source",
      transcript:
        '{"role":"assistant","content":[{"type":"text","text":"Stored answer"}],"openclawDelivery":{"sourceMessageId":"101"}}',
      expectedTarget: "101",
      expectedText: "Stored answer",
    },
  ])(
    "loads $name through final reply preparation",
    ({ transcript, expectedTarget, expectedText }) => {
      const stored: unknown = JSON.parse(transcript);
      const projected = projectAssistantTranscriptText(stored);
      expect(projected?.text).toBe("Stored answer");
      const payload = withAssistantReplySource(
        {
          text: projected?.text,
          ...(projected?.openclawDelivery?.replyToId
            ? { replyToId: projected.openclawDelivery.replyToId }
            : {}),
        },
        projected?.openclawDelivery?.sourceMessageId,
      );
      const finals = applyReplyThreading({
        payloads: [payload],
        replyToMode: "all",
        replyToChannel: "telegram",
        currentMessageId: "103",
      });
      expect(finals[0]?.replyToId).toBe(expectedTarget);
      expect(buildRecoverablePendingFinalDeliveryText(finals)).toBe(expectedText);
    },
  );

  it("refuses multi-payload media finals because text recovery loses payload boundaries", () => {
    expect(
      buildRecoverablePendingFinalDeliveryText([
        { text: "A", mediaUrl: "/tmp/a.png" },
        { text: "B", mediaUrl: "/tmp/b.png" },
      ]),
    ).toBeUndefined();
    expect(
      buildRecoverablePendingFinalDeliveryText([{ text: "A\nMEDIA:/tmp/a.png" }, { text: "B" }]),
    ).toBeUndefined();
  });

  it("preserves ordinary prose previously treated as silence", () => {
    expect(
      buildRecoverablePendingFinalDeliveryText([
        { text: "No channel reply." },
        { text: "visible final" },
      ]),
    ).toBe("No channel reply.\n\nvisible final");
    expect(buildRecoverablePendingFinalDeliveryText([{ text: "No channel reply." }])).toBe(
      "No channel reply.",
    );
  });
});
