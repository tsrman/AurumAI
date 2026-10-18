import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DragZone, MAX_FILE_BYTES } from "@/components/aurum/knowledge/DragZone";

function fileOf(name: string, size = 10, type = "application/pdf"): File {
  const file = new File(["x"], name, { type });
  // File.size is read-only; override it without allocating25MB just
  // to exercise the upload limit.
  Object.defineProperty(file, "size", { value: size });
  return file;
}

function dropFiles(files: File[]) {
  const zone = screen.getByLabelText("Document upload area");
  fireEvent.drop(zone, { dataTransfer: { files } });
}

describe("DragZone", () => {
  it("passes the real files up, not an invented name", () => {
    const onFiles = vi.fn();
    render(<DragZone onFiles={onFiles} />);

    const pdf = fileOf("regulation.pdf");
    dropFiles([pdf]);

    expect(onFiles).toHaveBeenCalledTimes(1);
    expect(onFiles.mock.calls[0][0]).toEqual([pdf]);
  });

  it("rejects an unsupported extension and says so", () => {
    const onFiles = vi.fn();
    render(<DragZone onFiles={onFiles} />);

    dropFiles([fileOf("payload.exe", 10, "application/octet-stream")]);

    expect(onFiles).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("Not supported");
  });

  it("rejects a file over the limit before any server request", () => {
    const onFiles = vi.fn();
    render(<DragZone onFiles={onFiles} />);

    dropFiles([fileOf("huge.pdf", MAX_FILE_BYTES + 1)]);

    expect(onFiles).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("Larger than 25 MB");
  });

  it("takes suitable files from a mixed set and reports the rejected ones", () => {
    const onFiles = vi.fn();
    render(<DragZone onFiles={onFiles} />);

    const good = fileOf("report.docx");
    dropFiles([good, fileOf("script.sh", 10, "text/x-sh")]);

    expect(onFiles).toHaveBeenCalledWith([good]);
    expect(screen.getByRole("alert")).toHaveTextContent("script.sh");
  });

  it("accepts nothing while uploading", () => {
    const onFiles = vi.fn();
    render(<DragZone onFiles={onFiles} disabled />);

    dropFiles([fileOf("regulation.pdf")]);

    expect(onFiles).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Document upload area")).toHaveAttribute(
      "aria-disabled",
      "true"
    );
  });
});
