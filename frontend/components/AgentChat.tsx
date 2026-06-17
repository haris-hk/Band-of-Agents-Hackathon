"use client";

import { useEffect, useRef } from "react";
import { agentAccentColor, ChatMessage } from "../lib/chatMessages";

function MessageBody({ text }: { text: string }) {
  // Render lines; bold **text** markers; inline code `text`
  const lines = text.split("\n");
  return (
    <div className="msgBody">
      {lines.map((line, li) => {
        const parts = line.split(/(`[^`]+`|\*\*[^*]+\*\*)/g);
        return (
          <p key={li} className={line.startsWith("  •") ? "msgBullet" : undefined}>
            {parts.map((part, pi) => {
              if (part.startsWith("**") && part.endsWith("**")) {
                return <strong key={pi}>{part.slice(2, -2)}</strong>;
              }
              if (part.startsWith("`") && part.endsWith("`")) {
                return <code key={pi} className="inlineCode">{part.slice(1, -1)}</code>;
              }
              return <span key={pi}>{part}</span>;
            })}
          </p>
        );
      })}
    </div>
  );
}

export function AgentChat({
  messages,
  isRunning,
}: {
  messages: ChatMessage[];
  isRunning?: boolean;
}) {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length]);

  if (messages.length === 0) {
    return (
      <div className="chatEmpty">
        <div className="chatEmptyIcon">🤖</div>
        <p>Run a pipeline to watch agents collaborate here.</p>
        <p className="muted">
          You&apos;ll see each agent explain what it&apos;s doing in plain English as the pipeline runs.
        </p>
      </div>
    );
  }

  return (
    <div className="chatRoom">
      {messages.map((message) => {
        if (message.variant === "system") {
          return (
            <div key={message.id} className="chatSystem">
              <span className="chatSystemIcon">⚡</span>
              <div className="chatSystemBody">
                <MessageBody text={message.text} />
              </div>
              {message.time ? <time className="chatTime">{message.time}</time> : null}
            </div>
          );
        }

        const accent = agentAccentColor(message.author);
        const bubbleClass =
          message.variant === "error"
            ? "chatBubble chatBubble-error"
            : message.variant === "success"
              ? "chatBubble chatBubble-success"
              : message.variant === "handoff"
                ? "chatBubble chatBubble-handoff"
                : "chatBubble";

        return (
          <article key={message.id} className="chatRow">
            <div
              className="chatAvatar"
              style={{ backgroundColor: accent }}
              aria-hidden
              title={message.author}
            >
              {message.initials}
            </div>
            <div className="chatContent">
              <header className="chatHeader">
                <strong>{message.author}</strong>
                {message.stage ? (
                  <span className="chatStage">{message.stage}</span>
                ) : null}
                {message.time ? (
                  <time className="chatTime">{message.time}</time>
                ) : null}
              </header>
              <div className={bubbleClass}>
                <MessageBody text={message.text} />
              </div>
            </div>
          </article>
        );
      })}

      {isRunning && (
        <div className="chatTyping">
          <span className="typingDot" />
          <span className="typingDot" />
          <span className="typingDot" />
          <span className="typingLabel">Agent processing…</span>
        </div>
      )}

      <div ref={endRef} />
    </div>
  );
}
