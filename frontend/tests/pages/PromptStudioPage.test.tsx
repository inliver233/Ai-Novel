// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PromptStudioPage } from "../../src/pages/PromptStudioPage";
import { usePromptStudio } from "../../src/pages/promptStudio/usePromptStudio";

vi.mock("../../src/pages/promptStudio/usePromptStudio", () => ({
  usePromptStudio: vi.fn(),
}));
vi.mock("../../src/pages/promptStudio/CategoryListPanel", () => ({
  CategoryListPanel: () => <div data-testid="category-list" />,
}));
vi.mock("../../src/pages/promptStudio/PresetEditorPanel", () => ({
  PresetEditorPanel: () => <div data-testid="preset-editor" />,
}));

const mockedUsePromptStudio = vi.mocked(usePromptStudio);

describe("PromptStudioPage builtin defaults sync", () => {
  const syncBuiltinDefaults = vi.fn(async () => true);

  beforeEach(() => {
    syncBuiltinDefaults.mockClear();
    mockedUsePromptStudio.mockReturnValue({
      projectId: "p1",
      categories: [],
      selectedCategoryKey: "",
      selectedCategory: null,
      selectedPresetId: null,
      selectedPresetSummary: null,
      presetDetail: null,
      draftName: "",
      draftContent: "",
      setDraftName: vi.fn(),
      setDraftContent: vi.fn(),
      loading: false,
      presetLoading: false,
      busy: false,
      loadError: null,
      presetError: null,
      hasChanges: false,
      loadCategories: vi.fn(async () => undefined),
      selectCategory: vi.fn(),
      selectPreset: vi.fn(),
      createPreset: vi.fn(async () => null),
      updatePreset: vi.fn(async () => null),
      deletePreset: vi.fn(async () => false),
      activatePreset: vi.fn(async () => null),
      syncBuiltinDefaults,
    });
  });

  it("exposes an editor action that invokes explicit builtin synchronization", async () => {
    const user = userEvent.setup();
    render(<PromptStudioPage />);

    await user.click(screen.getByRole("button", { name: "同步内置提示词" }));

    expect(syncBuiltinDefaults).toHaveBeenCalledTimes(1);
  });
});
