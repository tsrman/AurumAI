import { createTranslator } from "next-intl";
import { getApiErrorMessage, LocalizedError } from "@/lib/errors";
import en from "../../messages/en.json";
import ru from "../../messages/ru.json";

const t = createTranslator({ locale: "en", messages: en, namespace: "errors" });
const tRu = createTranslator({ locale: "ru", messages: ru, namespace: "errors" });

test("network errors get a localized message", () => {
  expect(getApiErrorMessage(new TypeError("Failed to fetch"), t)).toBe("Network error");
});

test("401/403 responses map to access denied", () => {
  expect(
    getApiErrorMessage({ response: new Response(null, { status: 401 }) }, t)
  ).toBe("Access denied");
  expect(
    getApiErrorMessage({ response: new Response(null, { status: 403 }) }, t)
  ).toBe("Access denied");
});

test("404 responses map to not found", () => {
  expect(
    getApiErrorMessage({ response: new Response(null, { status: 404 }) }, t)
  ).toBe("Not found");
});

test("409 responses explain that dependent data must be deleted first", () => {
  expect(
    getApiErrorMessage({ response: new Response(null, { status: 409 }) }, t)
  ).toBe("Delete the related data first");
});

test("other HTTP statuses fall back to a generic HTTP message", () => {
  expect(
    getApiErrorMessage({ response: new Response(null, { status: 500 }) }, t)
  ).toBe("HTTP 500");
});

test("plain Error instances surface their own message", () => {
  expect(getApiErrorMessage(new Error("boom"), t)).toBe("boom");
});

test("unrecognized values fall back to a generic message", () => {
  expect(getApiErrorMessage("not an error", t)).toBe("Unknown error");
});

test("localized errors resolve in the active locale", () => {
  expect(getApiErrorMessage(new LocalizedError("questionClosed"), t)).toBe(
    "This question no longer accepts answers"
  );
  expect(getApiErrorMessage(new LocalizedError("questionClosed"), tRu)).toBe(
    ru.errors.questionClosed
  );
  expect(getApiErrorMessage(new TypeError("Failed to fetch"), tRu)).toBe(ru.errors.network);
});
