import { renderHook, waitFor, act } from "@testing-library/react";
import * as React from "react";
import { vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  useGroups,
  useCreateGroup,
  useUpdateGroup,
  useDeleteGroup,
} from "@/hooks/useGroups";
import type { Group } from "@/lib/api";

const getGroups = vi.fn();
const createGroup = vi.fn();
const updateGroup = vi.fn();
const deleteGroup = vi.fn();

vi.mock("@/lib/api", () => ({
  getGroups: () => getGroups(),
  createGroup: (data: unknown) => createGroup(data),
  updateGroup: (id: string, data: unknown) => updateGroup(id, data),
  deleteGroup: (id: string) => deleteGroup(id),
}));

const group: Group = {
  id: "group-1",
  name: "support",
};

function Wrapper({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(
    () => new QueryClient({ defaultOptions: { queries: { retry: false } } })
  );
  return React.createElement(QueryClientProvider, { client }, children);
}

beforeEach(() => {
  getGroups.mockReset();
  createGroup.mockReset();
  updateGroup.mockReset();
  deleteGroup.mockReset();
});

test("useGroups exposes the fetched group list", async () => {
  getGroups.mockResolvedValue({ data: [group], meta: {} });

  const { result } = renderHook(() => useGroups(), { wrapper: Wrapper });

  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data?.data).toEqual([group]);
});

test("useCreateGroup calls createGroup and refetches the list", async () => {
  getGroups.mockResolvedValue({ data: [], meta: {} });
  createGroup.mockResolvedValue({ data: group, meta: {} });

  const { result } = renderHook(
    () => ({ groups: useGroups(), create: useCreateGroup() }),
    { wrapper: Wrapper }
  );
  await waitFor(() => expect(result.current.groups.isSuccess).toBe(true));

  getGroups.mockResolvedValue({ data: [group], meta: {} });
  await act(async () => {
    await result.current.create.mutateAsync({ name: "support" });
  });

  expect(createGroup).toHaveBeenCalledWith({ name: "support" });
  await waitFor(() => expect(getGroups).toHaveBeenCalledTimes(2));
});

test("useUpdateGroup calls updateGroup with id and payload", async () => {
  updateGroup.mockResolvedValue({ data: group, meta: {} });
  getGroups.mockResolvedValue({ data: [], meta: {} });

  const { result } = renderHook(() => useUpdateGroup(), { wrapper: Wrapper });
  await act(async () => {
    await result.current.mutateAsync({
      id: "group-1",
      data: { name: "renamed" },
    });
  });

  expect(updateGroup).toHaveBeenCalledWith("group-1", { name: "renamed" });
});

test("useDeleteGroup calls deleteGroup with the id", async () => {
  deleteGroup.mockResolvedValue(undefined);
  getGroups.mockResolvedValue({ data: [], meta: {} });

  const { result } = renderHook(() => useDeleteGroup(), { wrapper: Wrapper });
  await act(async () => {
    await result.current.mutateAsync("group-1");
  });

  expect(deleteGroup).toHaveBeenCalledWith("group-1");
});
