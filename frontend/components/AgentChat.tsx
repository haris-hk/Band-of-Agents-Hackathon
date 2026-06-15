"use client";

import { useEffect, useRef } from "react";
import { agentAccentColor, ChatMessage } from "../lib/chatMessages";

export function AgentChat({ messages }: { messages: ChatMessage[] }) {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length]);

  if (messages.length === 0) {
    return (
      <div className="chatEmpty">
        <p>Run a pipeline to watch agents collaborate here.</p>
        <p className="muted">Messages appear live as each stage completes.</p>
      </div>
    );
  }

  return (
    <div className="chatRoom">
      {messages.map((message) => {
        if (message.variant === "system") {
          return (
            <div key={message.id} className="chatSystem">
              <span>{message.text}</span>
              {message.time ? <time>{message.time}</time> : null}
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
            >
              {message.initials}
            </div>
            <div className="chatContent">
              <header className="chatHeader">
                <strong>{message.author}</strong>
                {message.stage ? (
                  <span className="chatStage">{message.stage}</span>
                ) : null}
                {message.time ? <time>{message.time}</time> : null}
              </header>
              <div className={bubbleClass}>{message.text}</div>
            </div>
          </article>
        );
      })}
      <div ref={endRef} />
    </div>
  );
}
