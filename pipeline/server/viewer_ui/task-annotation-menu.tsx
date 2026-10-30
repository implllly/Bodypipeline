"use client";

import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export interface SegmentMenuAnchor {
  x: number;
  y: number;
  trigger: HTMLElement;
}
export function segmentMenuAnchor(
  trigger: HTMLElement,
  x = 0,
  y = 0,
): SegmentMenuAnchor {
  const bounds = trigger.getBoundingClientRect();
  return {
    x: x || bounds.left + Math.min(bounds.width / 2, 80),
    y: y || bounds.bottom,
    trigger,
  };
}

export function SegmentContextMenu({
  anchor,
  label,
  onDelete,
  onClose,
}: {
  anchor: SegmentMenuAnchor;
  label: string;
  onDelete: () => void;
  onClose: () => void;
}) {
  const menu = useRef<HTMLDivElement>(null);
  const action = useRef<HTMLButtonElement>(null);
  const [position, setPosition] = useState({ x: anchor.x, y: anchor.y });
  const restoreFocus = () => {
    if (anchor.trigger.isConnected)
      anchor.trigger.focus({ preventScroll: true });
  };
  useLayoutEffect(() => {
    const bounds = menu.current?.getBoundingClientRect();
    if (!bounds) return;
    setPosition({
      x: Math.max(8, Math.min(anchor.x, window.innerWidth - bounds.width - 8)),
      y: Math.max(
        8,
        Math.min(anchor.y, window.innerHeight - bounds.height - 8),
      ),
    });
    action.current?.focus({ preventScroll: true });
  }, [anchor]);
  useEffect(() => {
    const outside = (event: PointerEvent) => {
      if (!menu.current?.contains(event.target as Node)) onClose();
    };
    const dismiss = () => onClose();
    window.addEventListener("pointerdown", outside, true);
    window.addEventListener("scroll", dismiss, true);
    window.addEventListener("resize", dismiss);
    window.addEventListener("blur", dismiss);
    return () => {
      window.removeEventListener("pointerdown", outside, true);
      window.removeEventListener("scroll", dismiss, true);
      window.removeEventListener("resize", dismiss);
      window.removeEventListener("blur", dismiss);
    };
  }, [onClose]);
  return createPortal(
    <div
      ref={menu}
      role="menu"
      aria-label="标注片段操作"
      className="ta-context-menu"
      style={{ left: position.x, top: position.y }}
      onContextMenu={(e) => e.preventDefault()}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          onClose();
          restoreFocus();
        }
        if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
          e.preventDefault();
          action.current?.focus();
        }
        if (e.key === "Tab") onClose();
      }}
    >
      <div className="ta-menu-caption">{label}</div>
      <button
        ref={action}
        role="menuitem"
        type="button"
        className="ta-menu-delete"
        onClick={() => {
          onDelete();
          onClose();
          restoreFocus();
        }}
      >
        <span aria-hidden="true">×</span>删除片段
      </button>
      <p>只删除这条标注，可撤销</p>
    </div>,
    document.body,
  );
}
