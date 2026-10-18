import { act } from "@testing-library/react";
import { vi } from "vitest";
import "@testing-library/jest-dom/vitest";

// jsdom does no real layout: getBoundingClientRect always reports an
// all-zero rect, and offsetWidth/offsetHeight are hardcoded to 0. @xyflow/
// react measures nodes via `element.offsetWidth/offsetHeight`
// (@xyflow/system's getDimensions) and handle positions via
// `getBoundingClientRect()` — with either stuck at zero, every node stays
// permanently "unmeasured" and its edges never render. No other suite in
// this repo depends on real layout.
Element.prototype.getBoundingClientRect = () => ({
  width: 160,
  height: 80,
  top: 0,
  left: 0,
  bottom: 80,
  right: 160,
  x: 0,
  y: 0,
  toJSON() {
    return this;
  },
});
Object.defineProperty(HTMLElement.prototype, "offsetWidth", {
  configurable: true,
  get: () => 160,
});
Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
  configurable: true,
  get: () => 80,
});

// jsdom doesn't implement ResizeObserver, and @xyflow/react needs its
// callback to actually fire once per observed node to mark that node
// "measured" — otherwise it stays permanently hidden and its edges never
// render (only the node/edge *data*, not the DOM, would reflect changes).
// jsdom's own getBoundingClientRect always reports 0x0, so report a
// plausible non-zero size instead of relaying it.
class ResizeObserverStub implements ResizeObserver {
  constructor(private readonly callback: ResizeObserverCallback) {}

  observe(target: Element): void {
    const rect = {
      width: 160,
      height: 80,
      top: 0,
      left: 0,
      bottom: 80,
      right: 160,
      x: 0,
      y: 0,
      toJSON: () => rect,
    };
    // Real ResizeObserver callbacks fire asynchronously, after every effect
    // from the same commit (including the ReactFlow wrapper's own effect
    // that records its DOM node) has already run. Firing synchronously here
    // races that wrapper effect and the library silently no-ops.
    queueMicrotask(() => {
      act(() => {
        this.callback(
          [
            {
              target,
              contentRect: rect as DOMRectReadOnly,
              borderBoxSize: [{ inlineSize: 160, blockSize: 80 }],
              contentBoxSize: [{ inlineSize: 160, blockSize: 80 }],
              devicePixelContentBoxSize: [{ inlineSize: 160, blockSize: 80 }],
            },
          ],
          this
        );
      });
    });
  }

  unobserve(): void {}
  disconnect(): void {}
}
Object.defineProperty(window, "ResizeObserver", {
  writable: true,
  value: ResizeObserverStub,
});

// jsdom doesn't implement elementFromPoint either; @xyflow/react's click-to-
// connect handle validation calls it to hit-test the target handle.
if (!document.elementFromPoint) {
  document.elementFromPoint = () => null;
}

// jsdom has no CSSOM geometry types at all; @xyflow/react reads the pane's
// zoom level via `new DOMMatrixReadOnly(style.transform).m22`. Tests never
// zoom, so identity (m22 = 1) is always correct here.
if (!("DOMMatrixReadOnly" in window)) {
  Object.defineProperty(window, "DOMMatrixReadOnly", {
    writable: true,
    value: class DOMMatrixReadOnlyStub {
      m22 = 1;
    },
  });
}

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

// Components resolve text through next-intl. Unit tests render them without a
// provider, so the hooks are backed by the English catalog and assertions check
// the exact English UI text. tests/i18n covers the Russian catalog.
vi.mock("next-intl", async (importOriginal) => {
  const actual = await importOriginal<typeof import("next-intl")>();
  const { default: messages } = await import("../messages/en.json");
  // Cache translators per namespace: next-intl returns stable functions, and
  // hooks list them as effect dependencies.
  const translators = new Map<string, ReturnType<typeof actual.createTranslator>>();
  return {
    ...actual,
    useTranslations: (namespace?: string) => {
      const key = namespace ?? "";
      if (!translators.has(key)) {
        translators.set(key, actual.createTranslator({ locale: "en", messages, namespace: namespace as never }));
      }
      return translators.get(key)!;
    },
    useLocale: () => "en",
    useFormatter: () => actual.createFormatter({ locale: "en", timeZone: "UTC" }),
  };
});
