import type { Page } from "@playwright/test";

/** Break one real response body, after headers, beyond the HTTP client's retries. */
export async function breakFirstTurnStream(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const original = window.fetch.bind(window);
    let broken = false;
    window.fetch = async (...args: Parameters<typeof fetch>) => {
      const response = await original(...args);
      if (broken || !response.ok || !/\/turns\/[^/]+\/events(?:\?|$)/.test(response.url)
        || !response.body) return response;
      broken = true;
      const reader = response.body.getReader();
      return new Response(new ReadableStream<Uint8Array>({
        async start(controller) {
          try {
            await reader.read();
            await reader.cancel();
          } finally {
            controller.error(new TypeError("simulated turn stream connection reset"));
          }
        },
      }), { status: response.status, headers: response.headers });
    };
  });
}
