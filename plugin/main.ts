import {
  App,
  ItemView,
  Notice,
  Plugin,
  PluginSettingTab,
  Setting,
  TFile,
  WorkspaceLeaf,
  normalizePath,
} from "obsidian";
import type { ViewStateResult } from "obsidian";
import { DEFAULT_ENDPOINT, McpClient } from "./client";
import { transport } from "./transport";

const SIDEBAR_VIEW_TYPE = "obsidian-thing-sidebar";
const MAP_VIEW_TYPE = "obsidian-thing-map";
const STATUS_VIEW_TYPE = "obsidian-thing-status";
const STATUS_COMMAND_ID = "show-index-status";

interface ThingSettings {
  endpoint: string;
  secretId: string;
}

const DEFAULT_SETTINGS: ThingSettings = {
  endpoint: "",
  secretId: "",
};

interface SectionMatch {
  score: number;
  headings?: string[];
  line_start?: number;
  line_end?: number;
  page?: number;
}

interface Match extends SectionMatch {
  path: string;
  sections?: SectionMatch[];
}

interface Matches {
  matches: Match[];
  vector_unavailable?: boolean;
}

interface FindResult {
  paths: string[];
}

interface MapPoint {
  path: string;
  x: number;
  y: number;
  score: number;
  source?: boolean;
}

interface MapResult {
  points: MapPoint[];
  explained: number[];
  dimensions: number;
}

interface IndexStatus {
  mode: string;
  files: number | null;
  excluded: number | null;
  indexed: number | null;
  unindexed: number | null;
  embedded: number | null;
  pending: number;
  last_full_scan: string | null;
}

interface EmbeddingStatus {
  provider: string;
  model: string;
  dimensions: number;
  profile: string;
  remote: boolean;
  can_send: boolean;
  daily_budget_usd: string | null;
  related_min_score: number;
  allowed_mime_types: string[];
}

interface CostTotals {
  calls: number;
  reported: string;
  estimated: string;
  unknown_calls: number;
  local_calls: number;
}

interface SpendPeriod extends CostTotals {
  name: string;
}

interface SpendDay extends CostTotals {
  date: string;
}

interface SpendReport {
  periods: SpendPeriod[];
  daily: SpendDay[];
}

interface StatusReport {
  index: IndexStatus;
  embeddings?: EmbeddingStatus;
  spend?: SpendReport;
}

interface FindFilters {
  tags?: string[];
  aliases?: string[];
  alias_property?: string;
  properties?: Record<string, string | null>;
  filename_regex?: string;
}

type SearchOutput =
  | { kind: "matches"; data: Matches }
  | { kind: "paths"; paths: string[] };

let viewInstance = 0;

function errorText(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return String(error);
}

function parseList(value: string): string[] {
  return value
    .split(",")
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
}

function parseLimit(value: string): number | null {
  const limit = Number.parseInt(value, 10);
  return Number.isFinite(limit) && limit >= 1 ? limit : null;
}

function parseOptionalLimit(value: string): number | null | undefined {
  if (!value.trim()) return undefined;
  return parseLimit(value);
}

function formatScore(score: number): string {
  return Number.isFinite(score) ? score.toFixed(3) : "-";
}

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return count === 1 ? singular : pluralForm;
}

function countText(value: number | null | undefined): string {
  return value === null || value === undefined ? "Not scanned yet" : String(value);
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "Never completed";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function periodLabel(name: string): string {
  if (name === "24h") return "Last 24 hours";
  if (name === "7d") return "Last 7 days";
  if (name === "30d") return "Last 30 days";
  if (name === "all") return "All recorded";
  return name;
}

function modeLabel(mode: string): string {
  if (mode === "sections") return "Sections";
  if (mode === "size") return "Whole files";
  return mode;
}

function toNumber(value: string): number {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

function sumAmounts(days: SpendDay[], key: "reported" | "estimated"): string {
  const total = days.reduce((sum, day) => sum + toNumber(day[key]), 0);
  return total.toFixed(4).replace(/\.?0+$/, "") || "0";
}

function emptyDay(date: string): SpendDay {
  return {
    date,
    calls: 0,
    reported: "0",
    estimated: "0",
    unknown_calls: 0,
    local_calls: 0,
  };
}

function lastThirtyDays(daily: SpendDay[]): SpendDay[] {
  const known = new Map(daily.map((day) => [day.date, day]));
  const now = new Date();
  const days: SpendDay[] = [];
  for (let offset = 29; offset >= 0; offset -= 1) {
    const date = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - offset));
    const key = date.toISOString().slice(0, 10);
    days.push(known.get(key) ?? emptyDay(key));
  }
  return days;
}

function matchMeta(match: SectionMatch): string {
  const parts = [`Score ${formatScore(match.score)}`];
  if (match.page !== undefined) parts.push(`Page ${match.page}`);
  if (match.line_start !== undefined) parts.push(`Line ${match.line_start}`);
  if (match.headings?.length) parts.push(match.headings.join(" > "));
  return parts.join(" | ");
}

function sectionMeta(section: SectionMatch): string {
  const parts = [`Score ${formatScore(section.score)}`];
  if (section.page !== undefined) parts.push(`Page ${section.page}`);
  if (section.line_start !== undefined) parts.push(`Line ${section.line_start}`);
  return parts.join(" | ");
}

function sectionLabel(section: SectionMatch): string {
  if (section.headings?.length) return section.headings.join(" > ");
  if (section.line_start !== undefined) return `Line ${section.line_start}`;
  return "Section";
}

function renderMatches(
  parent: HTMLElement,
  matches: Match[],
  plugin: ObsidianThingPlugin,
  emptyText: string,
): void {
  parent.empty();
  if (matches.length === 0) {
    parent.createEl("p", { cls: "thing-empty", text: emptyText });
    return;
  }
  const list = parent.createEl("ul", { cls: "thing-results" });
  for (const match of matches) {
    const item = list.createEl("li", { cls: "thing-result-item" });
    const button = item.createEl("button", {
      cls: "thing-result",
      attr: { type: "button", "aria-label": `Open ${match.path}` },
    });
    button.createSpan({ cls: "thing-result-path", text: match.path });
    button.createSpan({ cls: "thing-result-meta", text: matchMeta(match) });
    button.addEventListener("click", () => void plugin.openPath(match.path, match.line_start));
    // One section repeats the line the result itself already shows
    if (match.sections && match.sections.length > 1) {
      const sections = item.createEl("ul", { cls: "thing-sections" });
      for (const section of match.sections) {
        const sectionItem = sections.createEl("li");
        const sectionButton = sectionItem.createEl("button", {
          cls: "thing-section-button",
          attr: { type: "button", "aria-label": `Open ${match.path} at ${sectionLabel(section)}` },
        });
        sectionButton.createSpan({ cls: "thing-section-title", text: sectionLabel(section) });
        // The label already names the section, so the line and the score are enough
        sectionButton.createSpan({ cls: "thing-section-meta", text: sectionMeta(section) });
        sectionButton.addEventListener("click", () => void plugin.openPath(match.path, section.line_start));
      }
    }
  }
}

function renderPaths(
  parent: HTMLElement,
  paths: string[],
  plugin: ObsidianThingPlugin,
  emptyText: string,
): void {
  parent.empty();
  if (paths.length === 0) {
    parent.createEl("p", { cls: "thing-empty", text: emptyText });
    return;
  }
  const list = parent.createEl("ul", { cls: "thing-results" });
  for (const path of paths) {
    const item = list.createEl("li", { cls: "thing-result-item" });
    const button = item.createEl("button", {
      cls: "thing-result",
      attr: { type: "button", "aria-label": `Open ${path}` },
    });
    button.createSpan({ cls: "thing-result-path", text: path });
    button.addEventListener("click", () => void plugin.openPath(path));
  }
}

class ObsidianThingPlugin extends Plugin {
  settings: ThingSettings = { ...DEFAULT_SETTINGS };

  private toolsValue: Set<string> | null = null;
  private toolsPending: Promise<Set<string>> | null = null;
  private connectionVersion = 0;
  private statusCommandAdded = false;

  override async onload(): Promise<void> {
    await this.loadSettings();
    this.registerView(SIDEBAR_VIEW_TYPE, (leaf) => new SidebarView(leaf, this));
    this.registerView(MAP_VIEW_TYPE, (leaf) => new MapView(leaf, this));
    this.registerView(STATUS_VIEW_TYPE, (leaf) => new StatusView(leaf, this));
    this.addRibbonIcon("search", "Open Obsidian Thing", () => void this.activateSidebar());
    this.addCommand({
      id: "open-sidebar",
      name: "Open search and related notes",
      callback: () => void this.activateSidebar(),
    });
    this.addSettingTab(new ThingSettingTab(this.app, this));
    this.app.workspace.onLayoutReady(() => {
      void this.ensureTools().catch(() => undefined);
    });
  }

  override onunload(): void {
    this.app.workspace.detachLeavesOfType(SIDEBAR_VIEW_TYPE);
    this.app.workspace.detachLeavesOfType(MAP_VIEW_TYPE);
    this.app.workspace.detachLeavesOfType(STATUS_VIEW_TYPE);
  }

  private async loadSettings(): Promise<void> {
    const data = (await this.loadData()) as Partial<ThingSettings> | null;
    this.settings = {
      endpoint: typeof data?.endpoint === "string" ? data.endpoint : "",
      secretId: typeof data?.secretId === "string" ? data.secretId : "",
    };
  }

  async saveSettings(): Promise<void> {
    await this.saveData({
      endpoint: this.settings.endpoint,
      secretId: this.settings.secretId,
    });
  }

  endpoint(): string {
    return this.settings.endpoint.trim() || DEFAULT_ENDPOINT;
  }

  client(): McpClient {
    const secretId = this.settings.secretId;
    const token = secretId ? this.app.secretStorage.getSecret(secretId) : null;
    if (!token) {
      throw new Error("Select a bearer token secret in the Obsidian Thing settings");
    }
    return new McpClient(this.endpoint(), token, transport);
  }

  async ensureTools(force = false): Promise<Set<string>> {
    if (!force && this.toolsValue) return this.toolsValue;
    if (this.toolsPending) return this.toolsPending;
    const version = this.connectionVersion;
    const pending = this.client().tools();
    this.toolsPending = pending;
    try {
      const tools = await pending;
      if (version !== this.connectionVersion) {
        throw new Error("The connection settings changed; run the request again");
      }
      this.toolsValue = tools;
      this.syncStatusCommand(tools);
      return tools;
    } catch (error) {
      if (version === this.connectionVersion) this.toolsValue = null;
      throw error;
    } finally {
      if (this.toolsPending === pending) this.toolsPending = null;
    }
  }

  connectionChanged(): void {
    this.connectionVersion += 1;
    this.toolsValue = null;
    this.toolsPending = null;
    if (this.statusCommandAdded) {
      this.removeCommand(STATUS_COMMAND_ID);
      this.statusCommandAdded = false;
    }
    for (const leaf of this.app.workspace.getLeavesOfType(SIDEBAR_VIEW_TYPE)) {
      if (leaf.view instanceof SidebarView) leaf.view.connectionChanged();
    }
    for (const leaf of this.app.workspace.getLeavesOfType(MAP_VIEW_TYPE)) {
      if (leaf.view instanceof MapView) leaf.view.connectionChanged();
    }
    for (const leaf of this.app.workspace.getLeavesOfType(STATUS_VIEW_TYPE)) {
      if (leaf.view instanceof StatusView) leaf.view.connectionChanged();
    }
    void this.ensureTools().catch(() => undefined);
  }

  private syncStatusCommand(tools: Set<string>): void {
    const available = tools.has("obsidian.status");
    if (available && !this.statusCommandAdded) {
      this.addCommand({
        id: STATUS_COMMAND_ID,
        name: "Show index status",
        callback: () => void this.openStatus(),
      });
      this.statusCommandAdded = true;
    } else if (!available && this.statusCommandAdded) {
      this.removeCommand(STATUS_COMMAND_ID);
      this.statusCommandAdded = false;
    }
  }

  async activateSidebar(): Promise<void> {
    const existing = this.app.workspace.getLeavesOfType(SIDEBAR_VIEW_TYPE)[0];
    if (existing) {
      await this.app.workspace.revealLeaf(existing);
      return;
    }
    const leaf = this.app.workspace.getRightLeaf(false);
    if (!leaf) {
      new Notice("Obsidian Thing could not open a sidebar leaf");
      return;
    }
    await leaf.setViewState({ type: SIDEBAR_VIEW_TYPE, active: true });
    await this.app.workspace.revealLeaf(leaf);
  }

  async openMap(path: string, limit?: number): Promise<void> {
    const existing = this.app.workspace.getLeavesOfType(MAP_VIEW_TYPE)[0];
    const leaf = existing ?? this.app.workspace.getLeaf("tab");
    await leaf.setViewState({
      type: MAP_VIEW_TYPE,
      active: true,
      state: { path, limit: limit ?? null },
    });
    await leaf.loadIfDeferred();
    await this.app.workspace.revealLeaf(leaf);
    if (leaf.view instanceof MapView) await leaf.view.loadMapPage(path, limit);
  }

  async openStatus(): Promise<void> {
    const existing = this.app.workspace.getLeavesOfType(STATUS_VIEW_TYPE)[0];
    const leaf = existing ?? this.app.workspace.getLeaf("tab");
    await leaf.setViewState({ type: STATUS_VIEW_TYPE, active: true });
    await leaf.loadIfDeferred();
    await this.app.workspace.revealLeaf(leaf);
    if (leaf.view instanceof StatusView) await leaf.view.loadStatus();
  }

  async openPath(path: string, line?: number): Promise<void> {
    const file = this.app.vault.getAbstractFileByPath(normalizePath(path));
    if (!(file instanceof TFile)) {
      new Notice(`Obsidian Thing: cannot find ${path} in this vault`);
      return;
    }
    const openState = line === undefined ? undefined : { eState: { line: Math.max(0, line - 1) } };
    await this.app.workspace.getLeaf(false).openFile(file, openState);
  }
}

interface SearchState {
  output: SearchOutput | null;
  busy: boolean;
  sequence: number;
}

class SidebarView extends ItemView {
  private readonly plugin: ObsidianThingPlugin;
  private readonly instance = ++viewInstance;

  private tools: Set<string> | null = null;
  private toolsError: string | null = null;
  private toolsSequence = 0;
  private built = false;
  private eventsRegistered = false;
  private activeTab: "search" | "related" = "search";
  private activeFile: TFile | null = null;

  private readonly search: SearchState = { output: null, busy: false, sequence: 0 };
  private readonly related: SearchState & { source: string | null } = {
    output: null,
    busy: false,
    sequence: 0,
    source: null,
  };

  private connectionEl!: HTMLElement;
  private recheckButton!: HTMLButtonElement;
  private statusButton!: HTMLButtonElement;
  private tabListEl!: HTMLElement;
  private searchTabEl!: HTMLButtonElement;
  private relatedTabEl!: HTMLButtonElement;
  private searchPanelEl!: HTMLElement;
  private relatedPanelEl!: HTMLElement;

  private queryInput!: HTMLInputElement;
  private searchLimitInput!: HTMLInputElement;
  private searchButton!: HTMLButtonElement;
  private filtersEl!: HTMLDetailsElement;
  private tagsInput!: HTMLInputElement;
  private aliasesInput!: HTMLInputElement;
  private aliasPropertyInput!: HTMLInputElement;
  private propertiesInput!: HTMLTextAreaElement;
  private filenameRegexInput!: HTMLInputElement;
  private findButton!: HTMLButtonElement;
  private searchStatusEl!: HTMLElement;
  private searchResultsEl!: HTMLElement;

  private activeNoteEl!: HTMLElement;
  private relatedLimitInput!: HTMLInputElement;
  private excludeLinkedInput!: HTMLInputElement;
  private includePathsInput!: HTMLInputElement;
  private relatedButton!: HTMLButtonElement;
  private relatedStaleEl!: HTMLElement;
  private relatedStatusEl!: HTMLElement;
  private relatedHeaderEl!: HTMLElement;
  private relatedSourceEl!: HTMLElement;
  private mapButton!: HTMLButtonElement;
  private relatedResultsEl!: HTMLElement;

  constructor(leaf: WorkspaceLeaf, plugin: ObsidianThingPlugin) {
    super(leaf);
    this.plugin = plugin;
  }

  getViewType(): string {
    return SIDEBAR_VIEW_TYPE;
  }

  getDisplayText(): string {
    return "Obsidian Thing";
  }

  getIcon(): string {
    return "search";
  }

  protected override async onOpen(): Promise<void> {
    this.contentEl.empty();
    this.contentEl.addClass("obsidian-thing-view", "obsidian-thing-sidebar");
    this.build();
    this.built = true;
    this.updateActiveFile();
    this.applyTools();
    if (!this.eventsRegistered) {
      this.eventsRegistered = true;
      this.registerEvent(this.app.workspace.on("file-open", () => this.updateActiveFile()));
      this.registerEvent(this.app.workspace.on("active-leaf-change", () => this.updateActiveFile()));
    }
    await this.refreshTools(false);
  }

  protected override async onClose(): Promise<void> {
    this.search.sequence += 1;
    this.related.sequence += 1;
    this.toolsSequence += 1;
    this.search.busy = false;
    this.related.busy = false;
    this.search.output = null;
    this.related.output = null;
    this.related.source = null;
    this.built = false;
  }

  connectionChanged(): void {
    this.search.sequence += 1;
    this.related.sequence += 1;
    this.toolsSequence += 1;
    this.search.busy = false;
    this.related.busy = false;
    this.search.output = null;
    this.related.output = null;
    this.related.source = null;
    this.tools = null;
    this.toolsError = null;
    if (!this.built) return;
    this.setSearchStatus("", false);
    this.setRelatedStatus("", false);
    this.renderSearchOutput();
    this.renderRelatedOutput();
    this.applyTools();
    void this.refreshTools(true);
  }

  private build(): void {
    const header = this.contentEl.createDiv({ cls: "thing-header" });
    const titleRow = header.createDiv({ cls: "thing-title-row" });
    titleRow.createEl("h2", { cls: "thing-title", text: "Obsidian Thing" });
    const actions = titleRow.createDiv({ cls: "thing-header-actions" });
    this.statusButton = actions.createEl("button", {
      cls: "thing-small-button",
      text: "Status",
      attr: { type: "button", "data-thing": "open-status", "aria-label": "Open the index status tab" },
    });
    this.statusButton.addEventListener("click", () => void this.plugin.openStatus());
    this.recheckButton = actions.createEl("button", {
      cls: "thing-small-button",
      text: "Recheck",
      attr: { type: "button", "data-thing": "recheck", "aria-label": "Check the server tools again" },
    });
    this.recheckButton.addEventListener("click", () => void this.refreshTools(true));
    this.connectionEl = header.createEl("p", {
      cls: "thing-status",
      attr: { role: "status", "aria-live": "polite", "aria-atomic": "true", "data-thing": "connection" },
    });

    this.tabListEl = this.contentEl.createDiv({
      cls: "thing-tabs",
      attr: { role: "tablist", "aria-label": "Search and related notes" },
    });
    this.searchTabEl = this.createTab("Search", "search");
    this.relatedTabEl = this.createTab("Related", "related");
    this.tabListEl.addEventListener("keydown", (event) => this.handleTabKeys(event));

    this.searchPanelEl = this.contentEl.createEl("section", {
      cls: "thing-panel",
      attr: {
        role: "tabpanel",
        id: this.panelId("search"),
        "aria-labelledby": this.tabId("search"),
        tabindex: "0",
      },
    });
    this.buildSearchPanel(this.searchPanelEl);

    this.relatedPanelEl = this.contentEl.createEl("section", {
      cls: "thing-panel",
      attr: {
        role: "tabpanel",
        id: this.panelId("related"),
        "aria-labelledby": this.tabId("related"),
        tabindex: "0",
      },
    });
    this.buildRelatedPanel(this.relatedPanelEl);
    this.selectTab("search", false);
  }

  private tabId(tab: "search" | "related"): string {
    return `obsidian-thing-${this.instance}-${tab}-tab`;
  }

  private panelId(tab: "search" | "related"): string {
    return `obsidian-thing-${this.instance}-${tab}-panel`;
  }

  private createTab(label: string, tab: "search" | "related"): HTMLButtonElement {
    const button = this.tabListEl.createEl("button", {
      cls: "thing-tab",
      text: label,
      attr: {
        type: "button",
        role: "tab",
        id: this.tabId(tab),
        "aria-controls": this.panelId(tab),
        "aria-selected": "false",
        tabindex: "-1",
      },
    });
    button.addEventListener("click", () => this.selectTab(tab, false));
    return button;
  }

  private handleTabKeys(event: KeyboardEvent): void {
    if (!this.tools?.has("obsidian.related")) return;
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Home") {
      this.selectTab("search", true);
      return;
    }
    if (event.key === "End") {
      this.selectTab("related", true);
      return;
    }
    this.selectTab(this.activeTab === "search" ? "related" : "search", true);
  }

  private selectTab(tab: "search" | "related", focus: boolean): void {
    if (tab === "related" && !this.tools?.has("obsidian.related")) tab = "search";
    this.activeTab = tab;
    const searchActive = tab === "search";
    this.searchTabEl.setAttr("aria-selected", String(searchActive));
    this.relatedTabEl.setAttr("aria-selected", String(!searchActive));
    this.searchTabEl.setAttr("tabindex", searchActive ? "0" : "-1");
    this.relatedTabEl.setAttr("tabindex", searchActive ? "-1" : "0");
    this.searchPanelEl.hidden = !searchActive;
    this.relatedPanelEl.hidden = searchActive;
    if (focus) (searchActive ? this.searchTabEl : this.relatedTabEl).focus();
  }

  private buildSearchPanel(panel: HTMLElement): void {
    const queryField = panel.createDiv({ cls: "thing-field" });
    const queryId = `obsidian-thing-${this.instance}-query`;
    queryField.createEl("label", { text: "Text query", attr: { for: queryId } });
    this.queryInput = queryField.createEl("input", {
      attr: {
        id: queryId,
        type: "search",
        placeholder: "Words or a phrase",
        autocomplete: "off",
        "data-thing": "query",
      },
    });

    const controls = panel.createDiv({ cls: "thing-controls" });
    const limitField = controls.createDiv({ cls: "thing-field thing-field-inline" });
    const limitId = `obsidian-thing-${this.instance}-search-limit`;
    limitField.createEl("label", { text: "Results", attr: { for: limitId } });
    this.searchLimitInput = limitField.createEl("input", {
      attr: {
        id: limitId,
        type: "number",
        min: "1",
        step: "1",
        value: "10",
        placeholder: "default",
        title: "Leave empty to use the server default",
        "data-thing": "search-limit",
      },
    });
    this.searchButton = controls.createEl("button", {
      cls: "mod-cta",
      text: "Search",
      attr: { type: "button", "data-thing": "search", "aria-label": "Search the text index" },
    });
    this.searchButton.addEventListener("click", () => void this.runQuery());

    this.filtersEl = panel.createEl("details", { cls: "thing-filters", attr: { "data-thing": "filters" } });
    this.filtersEl.createEl("summary", { text: "Metadata filters" });
    this.filtersEl.createEl("p", {
      cls: "thing-hint",
      text: "Find needs at least one filter. Tags include child tags. Aliases ignore case. Write one property per line: name or name=value.",
    });
    this.tagsInput = this.buildField(this.filtersEl, "Tags, comma separated", "tags, projects", "tags");
    this.aliasesInput = this.buildField(this.filtersEl, "Aliases, comma separated", "Project Alpha", "aliases");
    this.aliasPropertyInput = this.buildField(this.filtersEl, "Alias property", "aliases", "alias-property");
    this.aliasPropertyInput.value = "aliases";

    const propertiesField = this.filtersEl.createDiv({ cls: "thing-field" });
    const propertiesId = `obsidian-thing-${this.instance}-properties`;
    propertiesField.createEl("label", { text: "Properties", attr: { for: propertiesId } });
    this.propertiesInput = propertiesField.createEl("textarea", {
      attr: {
        id: propertiesId,
        rows: "3",
        placeholder: "status\ncreated=2025-01-01",
        "data-thing": "properties",
      },
    });

    this.filenameRegexInput = this.buildField(
      this.filtersEl,
      "Filename regular expression",
      "^Project-.*\\.md$",
      "filename-regex",
    );

    this.findButton = this.filtersEl.createEl("button", {
      text: "Find by metadata",
      attr: { type: "button", "data-thing": "find", "aria-label": "Find notes by metadata filters" },
    });
    this.findButton.addEventListener("click", () => void this.runFind());

    this.searchStatusEl = panel.createEl("p", {
      cls: "thing-status",
      attr: { role: "status", "aria-live": "polite", "aria-atomic": "true", "data-thing": "search-status" },
    });
    this.searchResultsEl = panel.createDiv({ cls: "thing-results-box", attr: { "data-thing": "search-results" } });

    this.queryInput.addEventListener("input", () => this.updateControls());
    this.tagsInput.addEventListener("input", () => this.updateControls());
    this.aliasesInput.addEventListener("input", () => this.updateControls());
    this.propertiesInput.addEventListener("input", () => this.updateControls());
    this.filenameRegexInput.addEventListener("input", () => this.updateControls());
  }

  private buildField(
    parent: HTMLElement,
    label: string,
    placeholder: string,
    thing: string,
  ): HTMLInputElement {
    const field = parent.createDiv({ cls: "thing-field" });
    const id = `obsidian-thing-${this.instance}-${label.toLowerCase().replaceAll(/[^a-z]+/g, "-")}`;
    field.createEl("label", { text: label, attr: { for: id } });
    return field.createEl("input", {
      attr: { id, type: "text", placeholder, autocomplete: "off", "data-thing": thing },
    });
  }

  private buildRelatedPanel(panel: HTMLElement): void {
    const current = panel.createDiv({ cls: "thing-current" });
    current.createSpan({ cls: "thing-label", text: "Active note" });
    this.activeNoteEl = current.createSpan({ cls: "thing-current-path" });

    const controls = panel.createDiv({ cls: "thing-controls" });
    const limitField = controls.createDiv({ cls: "thing-field thing-field-inline" });
    const limitId = `obsidian-thing-${this.instance}-related-limit`;
    limitField.createEl("label", { text: "Results", attr: { for: limitId } });
    this.relatedLimitInput = limitField.createEl("input", {
      attr: {
        id: limitId,
        type: "number",
        min: "1",
        step: "1",
        value: "10",
        placeholder: "default",
        title: "Leave empty to use the server default",
        "data-thing": "related-limit",
      },
    });

    const excludeField = controls.createEl("label", { cls: "thing-check" });
    this.excludeLinkedInput = excludeField.createEl("input", {
      attr: { type: "checkbox", "data-thing": "exclude-linked" },
    });
    excludeField.createSpan({ text: "Exclude linked notes" });

    const pathsField = panel.createDiv({ cls: "thing-field" });
    const pathsId = `obsidian-thing-${this.instance}-include-paths`;
    pathsField.createEl("label", { text: "Only these paths", attr: { for: pathsId } });
    this.includePathsInput = pathsField.createEl("input", {
      attr: {
        id: pathsId,
        type: "text",
        placeholder: "notes, projects/alpha",
        autocomplete: "off",
        "data-thing": "include-paths",
      },
    });

    this.relatedButton = panel.createEl("button", {
      cls: "mod-cta",
      text: "Find related",
      attr: { type: "button", "data-thing": "related", "aria-label": "Find files related to the active note" },
    });
    this.relatedButton.addEventListener("click", () => void this.runRelated());

    this.relatedStaleEl = panel.createEl("p", { cls: "thing-hint thing-stale" });
    this.relatedStaleEl.hidden = true;
    this.relatedStatusEl = panel.createEl("p", {
      cls: "thing-status",
      attr: { role: "status", "aria-live": "polite", "aria-atomic": "true", "data-thing": "related-status" },
    });

    this.relatedHeaderEl = panel.createDiv({ cls: "thing-results-header" });
    this.relatedHeaderEl.hidden = true;
    this.relatedSourceEl = this.relatedHeaderEl.createEl("p", { cls: "thing-results-title" });
    this.mapButton = this.relatedHeaderEl.createEl("button", {
      text: "Open map",
      attr: { type: "button", "data-thing": "open-map", "aria-label": "Open the neighborhood map in a new tab" },
    });
    this.mapButton.addEventListener("click", () => void this.openMap());
    this.relatedResultsEl = panel.createDiv({ cls: "thing-results-box", attr: { "data-thing": "related-results" } });

    this.relatedLimitInput.addEventListener("input", () => this.updateControls());
    this.excludeLinkedInput.addEventListener("change", () => this.updateControls());
    this.includePathsInput.addEventListener("input", () => this.updateControls());
  }

  private updateActiveFile(): void {
    this.activeFile = this.app.workspace.getActiveFile();
    if (this.built) {
      this.activeNoteEl.setText(this.activeFile?.path ?? "No active note");
      this.updateStaleNotice();
      this.updateControls();
    }
  }

  private updateStaleNotice(): void {
    if (!this.built) return;
    const source = this.related.source;
    const activePath = this.activeFile?.path ?? null;
    if (source && source !== activePath) {
      this.relatedStaleEl.setText(`The active note changed. These results are for ${source}.`);
      this.relatedStaleEl.hidden = false;
    } else {
      this.relatedStaleEl.hidden = true;
    }
  }

  private async refreshTools(force: boolean): Promise<void> {
    if (!this.built) return;
    const sequence = ++this.toolsSequence;
    this.setConnection(force ? "Checking server tools..." : "Loading server tools...", false);
    this.recheckButton.disabled = true;
    try {
      const tools = await this.plugin.ensureTools(force);
      if (sequence !== this.toolsSequence || !this.built) return;
      this.tools = tools;
      this.toolsError = null;
    } catch (error) {
      if (sequence !== this.toolsSequence || !this.built) return;
      this.tools = null;
      this.toolsError = errorText(error);
    } finally {
      if (sequence === this.toolsSequence && this.built) this.recheckButton.disabled = false;
    }
    if (sequence !== this.toolsSequence || !this.built) return;
    this.applyTools();
  }

  private applyTools(): void {
    const ready = this.tools !== null;
    const relatedAvailable = ready && this.tools!.has("obsidian.related");
    const queryAvailable = ready && this.tools!.has("obsidian.query");
    const findAvailable = ready && this.tools!.has("obsidian.find");
    const statusAvailable = ready && this.tools!.has("obsidian.status");
    const mapAvailable = ready && this.tools!.has("obsidian.map");

    this.tabListEl.hidden = !relatedAvailable;
    this.relatedTabEl.hidden = !relatedAvailable;
    this.filtersEl.hidden = !findAvailable;
    this.statusButton.hidden = !statusAvailable;
    this.mapButton.hidden = !mapAvailable || !this.related.source;
    this.relatedHeaderEl.hidden = !this.related.source;
    if (relatedAvailable) {
      this.searchPanelEl.setAttr("role", "tabpanel");
      this.searchPanelEl.setAttr("aria-labelledby", this.tabId("search"));
      this.searchPanelEl.removeAttribute("aria-label");
    } else {
      this.searchPanelEl.setAttr("role", "region");
      this.searchPanelEl.setAttr("aria-label", "Search");
      this.searchPanelEl.removeAttribute("aria-labelledby");
    }

    if (!relatedAvailable && this.activeTab === "related") this.selectTab("search", false);

    if (this.toolsError) {
      this.setConnection(`Cannot reach the server: ${this.toolsError}`, true);
    } else if (!ready) {
      this.setConnection("Checking server tools...", false);
    } else {
      this.setConnection(`Connected. ${this.tools!.size} ${plural(this.tools!.size, "tool")} available.`, false);
    }

    if (ready && !queryAvailable && this.search.output === null) {
      this.setSearchStatus("This server does not provide text search.", true);
    }
    this.updateControls();
  }

  private updateControls(): void {
    if (!this.built) return;
    const ready = this.tools !== null;
    const queryAvailable = ready && this.tools!.has("obsidian.query");
    const findAvailable = ready && this.tools!.has("obsidian.find");
    const relatedAvailable = ready && this.tools!.has("obsidian.related");

    this.queryInput.disabled = !ready || !queryAvailable || this.search.busy;
    this.searchLimitInput.disabled = this.search.busy;
    this.searchButton.disabled = !ready || !queryAvailable || this.search.busy;
    this.findButton.disabled = !ready || !findAvailable || this.search.busy;
    this.tagsInput.disabled = this.search.busy;
    this.aliasesInput.disabled = this.search.busy;
    this.aliasPropertyInput.disabled = this.search.busy;
    this.propertiesInput.disabled = this.search.busy;
    this.filenameRegexInput.disabled = this.search.busy;

    this.relatedLimitInput.disabled = this.related.busy;
    this.excludeLinkedInput.disabled = this.related.busy;
    this.includePathsInput.disabled = this.related.busy;
    this.relatedButton.disabled = !ready || !relatedAvailable || this.related.busy || !this.activeFile;
    this.mapButton.hidden = !this.tools?.has("obsidian.map") || !this.related.source;
    this.searchResultsEl.setAttr("aria-busy", String(this.search.busy));
    this.relatedResultsEl.setAttr("aria-busy", String(this.related.busy));
  }

  private setConnection(text: string, error: boolean): void {
    this.connectionEl.setText(text);
    this.connectionEl.toggleClass("is-error", error);
  }

  private setSearchStatus(text: string, error: boolean): void {
    this.searchStatusEl.setText(text);
    this.searchStatusEl.toggleClass("is-error", error);
  }

  private setRelatedStatus(text: string, error: boolean): void {
    this.relatedStatusEl.setText(text);
    this.relatedStatusEl.toggleClass("is-error", error);
  }

  private requireTool(name: string, report: (text: string, error: boolean) => void): boolean {
    if (!this.tools) {
      report("Server tools are not loaded. Click Recheck.", true);
      return false;
    }
    if (!this.tools.has(name)) {
      report(`This server does not provide ${name}.`, true);
      return false;
    }
    return true;
  }

  private readLimit(
    input: HTMLInputElement,
    report: (text: string, error: boolean) => void,
  ): number | undefined | null {
    const limit = parseOptionalLimit(input.value);
    if (limit === null) {
      report("Use a result limit of 1 or more, or leave the field empty.", true);
      return null;
    }
    return limit;
  }

  private async runQuery(): Promise<void> {
    const text = this.queryInput.value.trim();
    if (!text) {
      this.setSearchStatus("Enter a text query.", true);
      return;
    }
    if (!this.requireTool("obsidian.query", (t, e) => this.setSearchStatus(t, e))) return;
    const limit = this.readLimit(this.searchLimitInput, (t, e) => this.setSearchStatus(t, e));
    if (limit === null) return;

    const sequence = ++this.search.sequence;
    this.search.busy = true;
    this.updateControls();
    this.setSearchStatus("Searching the text index...", false);
    const args: Record<string, unknown> = { text };
    if (limit !== undefined) args.limit = limit;
    try {
      const result = await this.plugin.client().call<Matches>("obsidian.query", args);
      if (sequence !== this.search.sequence) return;
      if (!Array.isArray(result?.matches)) throw new Error("The server returned an unexpected search response.");
      this.search.output = {
        kind: "matches",
        data: { matches: result.matches, vector_unavailable: result.vector_unavailable },
      };
      this.renderSearchOutput();
      if (result.vector_unavailable) {
        this.setSearchStatus("Semantic search is unavailable. Showing text matches only.", false);
      } else if (result.matches.length === 0) {
        this.setSearchStatus("No notes matched this query.", false);
      } else {
        this.setSearchStatus(`Found ${result.matches.length} ${plural(result.matches.length, "note")}.`, false);
      }
    } catch (error) {
      if (sequence !== this.search.sequence) return;
      this.search.output = null;
      this.renderSearchOutput();
      this.setSearchStatus(errorText(error), true);
    } finally {
      if (sequence === this.search.sequence) {
        this.search.busy = false;
        this.updateControls();
      }
    }
  }

  private readFilters(): FindFilters | null {
    const tags = parseList(this.tagsInput.value);
    const aliases = parseList(this.aliasesInput.value);
    const properties: Record<string, string | null> = {};
    for (const raw of this.propertiesInput.value.split("\n")) {
      const line = raw.trim();
      if (!line) continue;
      const separator = line.indexOf("=");
      if (separator < 0) {
        properties[line] = null;
        continue;
      }
      const name = line.slice(0, separator).trim();
      if (!name) throw new Error("Property names must not be empty.");
      properties[name] = line.slice(separator + 1).trim();
    }
    const filenameRegex = this.filenameRegexInput.value.trim();
    const count =
      tags.length + aliases.length + Object.keys(properties).length + (filenameRegex ? 1 : 0);
    if (count === 0) return null;
    const filters: FindFilters = {};
    if (tags.length) filters.tags = tags;
    if (aliases.length) {
      filters.aliases = aliases;
      filters.alias_property = this.aliasPropertyInput.value.trim() || "aliases";
    }
    if (Object.keys(properties).length) filters.properties = properties;
    if (filenameRegex) filters.filename_regex = filenameRegex;
    return filters;
  }

  private async runFind(): Promise<void> {
    if (!this.requireTool("obsidian.find", (t, e) => this.setSearchStatus(t, e))) return;
    let filters: FindFilters | null;
    try {
      filters = this.readFilters();
    } catch (error) {
      this.setSearchStatus(errorText(error), true);
      return;
    }
    if (!filters) {
      this.setSearchStatus("Add at least one metadata filter.", true);
      return;
    }
    const limit = this.readLimit(this.searchLimitInput, (t, e) => this.setSearchStatus(t, e));
    if (limit === null) return;

    const sequence = ++this.search.sequence;
    this.search.busy = true;
    this.updateControls();
    this.setSearchStatus("Finding notes by metadata...", false);
    const args: Record<string, unknown> = { ...filters };
    if (limit !== undefined) args.limit = limit;
    try {
      const result = await this.plugin.client().call<FindResult>("obsidian.find", args);
      if (sequence !== this.search.sequence) return;
      if (!Array.isArray(result?.paths)) throw new Error("The server returned an unexpected find response.");
      this.search.output = { kind: "paths", paths: result.paths };
      this.renderSearchOutput();
      this.setSearchStatus(
        result.paths.length === 0 ? "No notes match these filters." : `Found ${result.paths.length} ${plural(result.paths.length, "note")}.`,
        false,
      );
    } catch (error) {
      if (sequence !== this.search.sequence) return;
      this.search.output = null;
      this.renderSearchOutput();
      this.setSearchStatus(errorText(error), true);
    } finally {
      if (sequence === this.search.sequence) {
        this.search.busy = false;
        this.updateControls();
      }
    }
  }

  private async runRelated(): Promise<void> {
    const file = this.app.workspace.getActiveFile();
    if (!file) {
      this.setRelatedStatus("Open a note first.", true);
      return;
    }
    if (!this.requireTool("obsidian.related", (t, e) => this.setRelatedStatus(t, e))) return;
    const limit = this.readLimit(this.relatedLimitInput, (t, e) => this.setRelatedStatus(t, e));
    if (limit === null) return;

    const source = file.path;
    const includePaths = parseList(this.includePathsInput.value);
    const args: Record<string, unknown> = {
      path: source,
      exclude_linked: this.excludeLinkedInput.checked,
    };
    if (limit !== undefined) args.limit = limit;
    if (includePaths.length) args.include_paths = includePaths;

    const sequence = ++this.related.sequence;
    this.related.busy = true;
    this.updateControls();
    this.setRelatedStatus("Finding related files...", false);
    try {
      const result = await this.plugin.client().call<Matches>("obsidian.related", args);
      if (sequence !== this.related.sequence) return;
      if (!Array.isArray(result?.matches)) throw new Error("The server returned an unexpected related response.");
      this.related.source = source;
      this.related.output = { kind: "matches", data: { matches: result.matches } };
      this.renderRelatedOutput();
      this.updateStaleNotice();
      this.setRelatedStatus(
        result.matches.length === 0
          ? "No related files found."
          : `Found ${result.matches.length} related ${plural(result.matches.length, "file")}.`,
        false,
      );
    } catch (error) {
      if (sequence !== this.related.sequence) return;
      this.related.source = null;
      this.related.output = null;
      this.renderRelatedOutput();
      this.setRelatedStatus(errorText(error), true);
    } finally {
      if (sequence === this.related.sequence) {
        this.related.busy = false;
        this.updateControls();
      }
    }
  }

  private renderSearchOutput(): void {
    if (!this.built) return;
    this.searchResultsEl.empty();
    const output = this.search.output;
    if (!output) return;
    if (output.kind === "paths") {
      renderPaths(this.searchResultsEl, output.paths, this.plugin, "No notes match these filters.");
      return;
    }
    if (output.data.vector_unavailable) {
      this.searchResultsEl.createEl("p", {
        cls: "thing-note",
        text: "Semantic search is unavailable. These are text matches only.",
      });
    }
    const box = this.searchResultsEl.createDiv();
    renderMatches(box, output.data.matches, this.plugin, "No notes matched this query.");
  }

  private renderRelatedOutput(): void {
    if (!this.built) return;
    this.relatedResultsEl.empty();
    const source = this.related.source;
    if (!source) {
      this.relatedHeaderEl.hidden = true;
      return;
    }
    this.relatedHeaderEl.hidden = false;
    const count = this.related.output?.kind === "matches" ? this.related.output.data.matches.length : null;
    this.relatedSourceEl.setText(
      count === null ? `Results for ${source}` : `Results for ${source} (${count})`,
    );
    this.mapButton.hidden = !this.tools?.has("obsidian.map");
    if (this.related.output?.kind === "matches") {
      renderMatches(
        this.relatedResultsEl,
        this.related.output.data.matches,
        this.plugin,
        "No related files found.",
      );
    }
  }

  private async openMap(): Promise<void> {
    if (!this.related.source) return;
    const limit = parseOptionalLimit(this.relatedLimitInput.value);
    if (limit === null) {
      this.setRelatedStatus("Use a result limit of 1 or more, or leave the field empty.", true);
      return;
    }
    await this.plugin.openMap(this.related.source, limit);
  }
}

class MapView extends ItemView {
  private readonly plugin: ObsidianThingPlugin;
  private path: string | null = null;
  private limit: number | undefined;
  private sequence = 0;
  private busy = false;
  private built = false;

  private pathEl!: HTMLElement;
  private limitInput!: HTMLInputElement;
  private loadButton!: HTMLButtonElement;
  private statusEl!: HTMLElement;
  private summaryEl!: HTMLElement;
  private mapEl!: HTMLElement;
  private pointsEl!: HTMLElement;

  constructor(leaf: WorkspaceLeaf, plugin: ObsidianThingPlugin) {
    super(leaf);
    this.plugin = plugin;
  }

  getViewType(): string {
    return MAP_VIEW_TYPE;
  }

  getDisplayText(): string {
    return "Obsidian Thing map";
  }

  getIcon(): string {
    return "map";
  }

  override getState(): Record<string, unknown> {
    return { ...super.getState(), path: this.path ?? "", limit: this.limit ?? null };
  }

  override async setState(state: unknown, result: ViewStateResult): Promise<void> {
    const record = state && typeof state === "object" ? (state as Record<string, unknown>) : {};
    this.path = typeof record.path === "string" && record.path ? record.path : null;
    const raw = record.limit;
    const parsed =
      typeof raw === "number"
        ? raw
        : typeof raw === "string" && raw.trim()
          ? Number.parseInt(raw, 10)
          : Number.NaN;
    this.limit = Number.isFinite(parsed) && parsed >= 1 ? parsed : undefined;
    await super.setState(state, result);
    if (this.built) this.renderHeader();
  }

  protected override async onOpen(): Promise<void> {
    this.contentEl.empty();
    this.contentEl.addClass("obsidian-thing-view", "obsidian-thing-map-view");
    this.build();
    this.built = true;
    this.renderHeader();
  }

  protected override async onClose(): Promise<void> {
    this.sequence += 1;
    this.busy = false;
    this.built = false;
  }

  connectionChanged(): void {
    this.sequence += 1;
    this.busy = false;
    if (!this.built) return;
    this.mapEl.empty();
    this.pointsEl.empty();
    this.summaryEl.empty();
    this.setStatus("Connection changed. Choose Load map to refresh.", false);
    this.updateControls();
  }

  async loadMapPage(path: string, limit?: number): Promise<void> {
    this.path = path;
    this.limit = limit;
    if (!this.built) return;
    this.limitInput.value = limit === undefined ? "" : String(limit);
    this.renderHeader();
    await this.loadMap();
  }

  private build(): void {
    const header = this.contentEl.createDiv({ cls: "thing-header" });
    header.createEl("h2", { cls: "thing-title", text: "Neighborhood map" });
    this.pathEl = header.createEl("p", { cls: "thing-map-path" });

    const controls = this.contentEl.createDiv({ cls: "thing-controls" });
    const limitField = controls.createDiv({ cls: "thing-field thing-field-inline" });
    const limitId = `obsidian-thing-map-${++viewInstance}-limit`;
    limitField.createEl("label", { text: "Neighbors", attr: { for: limitId } });
    this.limitInput = limitField.createEl("input", {
      attr: {
        id: limitId,
        type: "number",
        min: "1",
        step: "1",
        value: "10",
        placeholder: "default",
        title: "Leave empty to use the server default",
        "data-thing": "map-limit",
      },
    });
    this.loadButton = controls.createEl("button", {
      cls: "mod-cta",
      text: "Load map",
      attr: { type: "button", "data-thing": "map-load", "aria-label": "Load the neighborhood map" },
    });
    this.loadButton.addEventListener("click", () => void this.loadFromControls());

    this.statusEl = this.contentEl.createEl("p", {
      cls: "thing-status",
      attr: { role: "status", "aria-live": "polite", "aria-atomic": "true", "data-thing": "map-status" },
    });
    this.summaryEl = this.contentEl.createDiv({ cls: "thing-map-summary", attr: { "data-thing": "map-summary" } });
    this.mapEl = this.contentEl.createDiv({ cls: "thing-map-box", attr: { "data-thing": "map" } });
    this.pointsEl = this.contentEl.createDiv({ cls: "thing-map-points", attr: { "data-thing": "map-points" } });
    this.limitInput.addEventListener("input", () => this.updateControls());
  }

  private renderHeader(): void {
    this.pathEl.setText(this.path ?? "No note selected");
    this.limitInput.value = this.limit === undefined ? "" : String(this.limit);
    this.updateControls();
  }

  private updateControls(): void {
    if (!this.built) return;
    this.limitInput.disabled = this.busy || !this.path;
    this.loadButton.disabled = this.busy || !this.path;
  }

  private setStatus(text: string, error: boolean): void {
    this.statusEl.setText(text);
    this.statusEl.toggleClass("is-error", error);
  }

  private async loadFromControls(): Promise<void> {
    const limit = parseOptionalLimit(this.limitInput.value);
    if (limit === null) {
      this.setStatus("Use a neighbor limit of 1 or more, or leave the field empty.", true);
      return;
    }
    this.limit = limit;
    await this.loadMap();
  }

  private async loadMap(): Promise<void> {
    if (!this.path) {
      this.setStatus("No note selected.", true);
      return;
    }
    const path = this.path;
    const sequence = ++this.sequence;
    this.busy = true;
    this.updateControls();
    this.setStatus("Building the neighborhood map...", false);
    this.mapEl.empty();
    this.pointsEl.empty();
    this.summaryEl.empty();
    try {
      const tools = await this.plugin.ensureTools();
      if (sequence !== this.sequence) return;
      if (!tools.has("obsidian.map")) throw new Error("This server does not provide the neighborhood map.");
      const args: Record<string, unknown> = { path };
      if (this.limit !== undefined) args.limit = this.limit;
      const result = await this.plugin.client().call<MapResult>("obsidian.map", args);
      if (sequence !== this.sequence) return;
      if (!Array.isArray(result?.points) || result.points.length === 0) {
        throw new Error("The server returned an unexpected map response.");
      }
      this.renderMap(result);
      const neighbors = result.points.length - 1;
      this.setStatus(`Mapped ${neighbors} ${plural(neighbors, "neighbor")}.`, false);
    } catch (error) {
      if (sequence !== this.sequence) return;
      this.setStatus(errorText(error), true);
    } finally {
      if (sequence === this.sequence) {
        this.busy = false;
        this.updateControls();
      }
    }
  }

  private renderMap(result: MapResult): void {
    this.summaryEl.empty();
    const explained = Array.isArray(result.explained) ? result.explained : [];
    const first = explained[0] === undefined ? null : `${(explained[0] * 100).toFixed(1)}%`;
    const second = explained[1] === undefined ? null : `${(explained[1] * 100).toFixed(1)}%`;
    const summary = this.summaryEl.createEl("p", { cls: "thing-hint" });
    summary.setText(
      `Source: ${this.path}. ` +
        (first === null
          ? "The map has no explained variance."
          : `Explained variance: ${first} on axis 1, ${second ?? "0.0%"} on axis 2.`) +
        ` Embedding dimensions: ${result.dimensions}.`,
    );

    this.mapEl.empty();
    const width = 520;
    const height = 520;
    const pad = 48;
    const xs = result.points.map((point) => point.x);
    const ys = result.points.map((point) => point.y);
    let minX = Math.min(...xs);
    let maxX = Math.max(...xs);
    let minY = Math.min(...ys);
    let maxY = Math.max(...ys);
    if (maxX - minX < 0.000001) {
      minX -= 0.5;
      maxX += 0.5;
    }
    if (maxY - minY < 0.000001) {
      minY -= 0.5;
      maxY += 0.5;
    }
    const px = (value: number) => pad + ((value - minX) / (maxX - minX)) * (width - pad * 2);
    const py = (value: number) => height - pad - ((value - minY) / (maxY - minY)) * (height - pad * 2);

    const svg = this.mapEl.createSvg("svg", {
      cls: "thing-map",
      attr: {
        viewBox: `0 0 ${width} ${height}`,
        role: "img",
        "aria-label": `Neighborhood map of ${this.path} with ${result.points.length - 1} neighbors. Use the file list for keyboard access.`,
      },
    });
    if (minX <= 0 && maxX >= 0) {
      svg.createSvg("line", {
        cls: "thing-map-axis",
        attr: { x1: px(0), y1: pad, x2: px(0), y2: height - pad },
      });
    }
    if (minY <= 0 && maxY >= 0) {
      svg.createSvg("line", {
        cls: "thing-map-axis",
        attr: { x1: pad, y1: py(0), x2: width - pad, y2: py(0) },
      });
    }
    svg
      .createSvg("text", {
        cls: "thing-map-axis-label",
        attr: { x: width / 2, y: height - 12, "text-anchor": "middle" },
      })
      .setText("Principal component 1");
    svg
      .createSvg("text", {
        cls: "thing-map-axis-label",
        attr: {
          x: 14,
          y: height / 2,
          "text-anchor": "middle",
          transform: `rotate(-90 14 ${height / 2})`,
        },
      })
      .setText("Principal component 2");

    const ordered = [...result.points].sort((a, b) => Number(Boolean(a.source)) - Number(Boolean(b.source)));
    for (const point of ordered) {
      const circle = svg.createSvg("circle", {
        cls: "thing-map-point",
        attr: {
          cx: px(point.x),
          cy: py(point.y),
          r: point.source ? 10 : 6,
          tabindex: "0",
          focusable: "true",
          role: "button",
          "aria-label": `Open ${point.path}, score ${formatScore(point.score)}`,
        },
      });
      if (point.source) circle.addClass("is-source");
      circle.createSvg("title").setText(`${point.path} | score ${formatScore(point.score)}`);
      circle.addEventListener("click", () => void this.plugin.openPath(point.path));
      circle.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          void this.plugin.openPath(point.path);
        }
      });
      if (point.source) {
        const name = point.path.split("/").pop() ?? point.path;
        const x = px(point.x);
        svg
          .createSvg("text", {
            cls: "thing-map-label",
            attr: {
              x: x > width - 160 ? x - 14 : x + 14,
              y: Math.max(16, py(point.y) - 12),
              "text-anchor": x > width - 160 ? "end" : "start",
            },
          })
          .setText(name.length > 30 ? `${name.slice(0, 27)}...` : name);
      }
    }

    this.pointsEl.empty();
    this.pointsEl.createEl("h3", { text: "Files on this map" });
    const list = this.pointsEl.createEl("ul", { cls: "thing-results" });
    for (const point of result.points) {
      const item = list.createEl("li", { cls: "thing-result-item" });
      const button = item.createEl("button", {
        cls: "thing-result",
        attr: { type: "button", "aria-label": `Open ${point.path}` },
      });
      button.createSpan({ cls: "thing-result-path", text: point.path });
      button.createSpan({
        cls: "thing-result-meta",
        text: `${point.source ? "Source" : "Neighbor"} | Score ${formatScore(point.score)}`,
      });
      button.addEventListener("click", () => void this.plugin.openPath(point.path));
    }
  }
}

class StatusView extends ItemView {
  private readonly plugin: ObsidianThingPlugin;
  private sequence = 0;
  private built = false;
  private busy = false;

  private statusEl!: HTMLElement;
  private refreshButton!: HTMLButtonElement;
  private bodyEl!: HTMLElement;

  constructor(leaf: WorkspaceLeaf, plugin: ObsidianThingPlugin) {
    super(leaf);
    this.plugin = plugin;
  }

  getViewType(): string {
    return STATUS_VIEW_TYPE;
  }

  getDisplayText(): string {
    return "Obsidian Thing status";
  }

  getIcon(): string {
    return "activity";
  }

  protected override async onOpen(): Promise<void> {
    this.contentEl.empty();
    this.contentEl.addClass("obsidian-thing-view", "obsidian-thing-status-view");
    this.build();
    this.built = true;
    await this.loadStatus();
  }

  protected override async onClose(): Promise<void> {
    this.sequence += 1;
    this.busy = false;
    this.built = false;
  }

  connectionChanged(): void {
    this.sequence += 1;
    this.busy = false;
    if (this.built) void this.loadStatus();
  }

  private build(): void {
    const header = this.contentEl.createDiv({ cls: "thing-header" });
    const titleRow = header.createDiv({ cls: "thing-title-row" });
    titleRow.createEl("h2", { cls: "thing-title", text: "Index status" });
    this.refreshButton = titleRow.createEl("button", {
      cls: "thing-small-button",
      text: "Refresh",
      attr: { type: "button", "data-thing": "status-refresh", "aria-label": "Refresh index status" },
    });
    this.refreshButton.addEventListener("click", () => void this.loadStatus());
    this.statusEl = header.createEl("p", {
      cls: "thing-status",
      attr: { role: "status", "aria-live": "polite", "aria-atomic": "true", "data-thing": "status" },
    });
    this.bodyEl = this.contentEl.createDiv({ cls: "thing-status-body", attr: { "data-thing": "status-body" } });
  }

  private setStatus(text: string, error: boolean): void {
    this.statusEl.setText(text);
    this.statusEl.toggleClass("is-error", error);
  }

  async loadStatus(): Promise<void> {
    if (!this.built) return;
    const sequence = ++this.sequence;
    this.busy = true;
    this.refreshButton.disabled = true;
    this.setStatus("Loading index status...", false);
    try {
      const tools = await this.plugin.ensureTools();
      if (sequence !== this.sequence) return;
      if (!tools.has("obsidian.status")) {
        this.bodyEl.empty();
        this.bodyEl.createEl("p", {
          cls: "thing-empty",
          text: "This server does not provide index status. Semantic search may be disabled.",
        });
        this.setStatus("Index status is unavailable.", true);
        return;
      }
      const result = await this.plugin.client().call<StatusReport>("obsidian.status");
      if (sequence !== this.sequence) return;
      if (!result || typeof result !== "object" || !result.index) {
        throw new Error("The server returned an unexpected status response.");
      }
      this.renderStatus(result);
      this.setStatus("Status updated.", false);
    } catch (error) {
      if (sequence !== this.sequence) return;
      this.bodyEl.empty();
      this.bodyEl.createEl("p", { cls: "thing-empty", text: errorText(error) });
      this.setStatus("Could not load index status.", true);
    } finally {
      if (sequence === this.sequence) {
        this.busy = false;
        this.refreshButton.disabled = false;
      }
    }
  }

  private renderStatus(status: StatusReport): void {
    this.bodyEl.empty();
    this.renderCoverage(status.index);
    if (status.embeddings) this.renderEmbeddings(status.embeddings);
    if (status.spend) this.renderSpend(status.spend);
    if (!status.embeddings && !status.spend) {
      this.bodyEl.createEl("p", {
        cls: "thing-note",
        text: "This vault has no embedding profile. Semantic search and cost reporting are off.",
      });
    }
  }

  private renderCoverage(index: IndexStatus): void {
    const section = this.bodyEl.createEl("section", { cls: "thing-section" });
    section.createEl("h3", { text: "Index coverage" });
    section.createEl("p", {
      cls: "thing-note",
      text:
        "Coverage counts describe the last completed full scan. They stay unchanged until the next full scan. " +
        "The pending queue is current.",
    });
    const table = this.createTable(section, "Index coverage from the last full scan", ["Metric", "Value"]);
    const body = table.createEl("tbody");
    this.addRow(body, "Mode", modeLabel(index.mode));
    this.addRow(body, "Files in scope", countText(index.files));
    this.addRow(body, "Excluded by policy", countText(index.excluded));
    this.addRow(body, "Indexed", countText(index.indexed));
    this.addRow(body, "Not indexed", countText(index.unindexed));
    this.addRow(body, "With embeddings", countText(index.embedded));
    this.addRow(body, "Pending embedding queue", String(index.pending ?? 0));
    this.addRow(body, "Last full scan", formatTimestamp(index.last_full_scan));
  }

  private renderEmbeddings(embeddings: EmbeddingStatus): void {
    const section = this.bodyEl.createEl("section", { cls: "thing-section" });
    section.createEl("h3", { text: "Embedding profile" });
    const table = this.createTable(section, "Embedding profile", ["Setting", "Value"]);
    const body = table.createEl("tbody");
    this.addRow(body, "Provider", embeddings.provider);
    this.addRow(body, "Model", embeddings.model);
    this.addRow(body, "Dimensions", String(embeddings.dimensions));
    this.addRow(body, "Profile", embeddings.profile);
    this.addRow(body, "Remote provider", embeddings.remote ? "Yes" : "No");
    this.addRow(body, "Can send input", embeddings.can_send ? "Yes" : "No");
    this.addRow(body, "Daily budget", embeddings.daily_budget_usd ?? "No limit");
    this.addRow(body, "Related minimum score", String(embeddings.related_min_score));
    this.addRow(
      body,
      "Allowed MIME types",
      Array.isArray(embeddings.allowed_mime_types) && embeddings.allowed_mime_types.length > 0
        ? embeddings.allowed_mime_types.join(", ")
        : "None",
    );
    if (!embeddings.can_send) {
      section.createEl("p", {
        cls: "thing-note",
        text: "The server blocks provider calls until its configuration allows them.",
      });
    }
  }

  private renderSpend(spend: SpendReport): void {
    const section = this.bodyEl.createEl("section", { cls: "thing-section" });
    section.createEl("h3", { text: "Recorded costs" });
    section.createEl("p", {
      cls: "thing-note",
      text:
        "Reported is a price returned by the provider. Estimated uses the configured rate. " +
        "Unpriced calls have no usable price. Local calls make no API charge.",
    });
    const periods = Array.isArray(spend.periods) ? spend.periods : [];
    const periodsTable = this.createTable(section, "Costs by period", [
      "Period",
      "Calls",
      "Reported, USD",
      "Estimated, USD",
      "Unpriced calls",
      "Local calls",
    ]);
    const periodsBody = periodsTable.createEl("tbody");
    for (const period of periods) {
      const row = periodsBody.createEl("tr");
      this.addCell(row, periodLabel(period.name), "row");
      this.addCell(row, String(period.calls), "number");
      this.addCell(row, period.reported, "number");
      this.addCell(row, period.estimated, "number");
      this.addCell(row, String(period.unknown_calls), "number");
      this.addCell(row, String(period.local_calls), "number");
    }
    if (periods.length === 0) {
      const row = periodsBody.createEl("tr");
      const cell = row.createEl("td", { attr: { colspan: "6" }, text: "No recorded calls." });
      cell.addClass("thing-empty-cell");
    }

    const daily = Array.isArray(spend.daily) ? spend.daily : [];
    this.renderDailyChart(section, daily);
  }

  private renderDailyChart(parent: HTMLElement, daily: SpendDay[]): void {
    const days = lastThirtyDays(daily);
    parent.createEl("h3", { text: "Daily cost, last 30 days" });
    const max = Math.max(
      0,
      ...days.map((day) => toNumber(day.reported) + toNumber(day.estimated)),
    );
    if (max <= 0) {
      parent.createEl("p", {
        cls: "thing-note",
        text: "No reported or estimated provider cost in the last 30 days.",
      });
    } else {
      const reportedTotal = sumAmounts(days, "reported");
      const estimatedTotal = sumAmounts(days, "estimated");
      const chart = parent.createDiv({
        cls: "thing-chart",
        attr: {
          role: "img",
          "aria-label": `Daily reported and estimated cost for the last 30 days. Total reported ${reportedTotal} USD. Total estimated ${estimatedTotal} USD.`,
        },
      });
      const bars = chart.createDiv({ cls: "thing-chart-bars", attr: { "aria-hidden": "true" } });
      days.forEach((day, index) => {
        const reported = toNumber(day.reported);
        const estimated = toNumber(day.estimated);
        const total = reported + estimated;
        const column = bars.createDiv({ cls: "thing-chart-day" });
        column.setAttr(
          "title",
          `${day.date}: reported ${day.reported} USD, estimated ${day.estimated} USD, ${day.calls} calls`,
        );
        const stack = column.createDiv({ cls: "thing-chart-stack" });
        stack.style.height = `${(total / max) * 100}%`;
        if (total > 0) {
          if (reported > 0) {
            const segment = stack.createDiv({ cls: "thing-chart-segment is-reported" });
            segment.style.height = `${(reported / total) * 100}%`;
          }
          if (estimated > 0) {
            const segment = stack.createDiv({ cls: "thing-chart-segment is-estimated" });
            segment.style.height = `${(estimated / total) * 100}%`;
          }
        }
        const showLabel = index % 5 === 0 || index === days.length - 1;
        column.createSpan({
          cls: "thing-chart-label",
          text: showLabel ? day.date.slice(5) : "",
        });
      });
      const legend = parent.createEl("p", { cls: "thing-legend" });
      legend.createSpan({ cls: "thing-swatch is-reported" });
      legend.createSpan({ text: "Reported provider price" });
      legend.createSpan({ cls: "thing-swatch is-estimated" });
      legend.createSpan({ text: "Estimated from the configured rate" });
    }

    const details = parent.createEl("details", { cls: "thing-daily" });
    details.createEl("summary", { text: "Daily totals" });
    const table = this.createTable(details, "Daily cost for the last 30 days", [
      "Date",
      "Calls",
      "Reported, USD",
      "Estimated, USD",
      "Unpriced calls",
      "Local calls",
    ]);
    const body = table.createEl("tbody");
    for (const day of days) {
      const row = body.createEl("tr");
      this.addCell(row, day.date, "row");
      this.addCell(row, String(day.calls), "number");
      this.addCell(row, day.reported, "number");
      this.addCell(row, day.estimated, "number");
      this.addCell(row, String(day.unknown_calls), "number");
      this.addCell(row, String(day.local_calls), "number");
    }
  }

  private createTable(
    parent: HTMLElement,
    caption: string,
    columns: string[],
  ): HTMLTableElement {
    const table = parent.createEl("table", { cls: "thing-table" });
    table.createEl("caption", { text: caption });
    const head = table.createEl("thead");
    const row = head.createEl("tr");
    for (const column of columns) row.createEl("th", { text: column, attr: { scope: "col" } });
    return table;
  }

  private addRow(body: HTMLElement, label: string, value: string): void {
    const row = body.createEl("tr");
    this.addCell(row, label, "row");
    this.addCell(row, value, "text");
  }

  private addCell(row: HTMLTableRowElement, text: string, kind: "row" | "text" | "number"): void {
    if (kind === "row") {
      row.createEl("th", { text, attr: { scope: "row" } });
      return;
    }
    const cell = row.createEl("td", { text });
    if (kind === "number") cell.addClass("thing-number");
  }
}

class ThingSettingTab extends PluginSettingTab {
  private readonly thing: ObsidianThingPlugin;
  private testStatusEl: HTMLElement | null = null;
  private testButton: HTMLButtonElement | null = null;

  constructor(app: App, plugin: ObsidianThingPlugin) {
    super(app, plugin);
    this.thing = plugin;
  }

  display(): void {
    const { containerEl } = this;
    containerEl.empty();
    containerEl.addClass("obsidian-thing-settings");
    containerEl.createEl("h2", { text: "Obsidian Thing" });
    containerEl.createEl("p", {
      cls: "setting-item-description",
      text:
        "Connect this vault to one Obsidian Thing MCP endpoint. " +
        "The bearer token stays in Obsidian's secret storage and is never written to data.json.",
    });

    new Setting(containerEl)
      .setName("MCP endpoint")
      .setDesc(`One endpoint for this vault. Leave empty to use ${DEFAULT_ENDPOINT}.`)
      .addText((text) => {
        text.setPlaceholder(DEFAULT_ENDPOINT).setValue(this.thing.settings.endpoint);
        text.inputEl.setAttr("data-thing", "endpoint");
        text.onChange((value) => {
          this.thing.settings.endpoint = value.trim();
        });
        text.inputEl.addEventListener("change", () => void this.saveConnection());
      });

    new Setting(containerEl)
      .setName("Bearer token secret")
      .setDesc(
        "Select a secret that holds this vault's token. Create the secret in Obsidian's secret storage first.",
      )
      .addDropdown((dropdown) => {
        dropdown.selectEl.setAttr("data-thing", "secret");
        const ids = this.app.secretStorage.listSecrets().slice().sort();
        dropdown.addOption("", "Select a secret");
        for (const id of ids) dropdown.addOption(id, id);
        if (this.thing.settings.secretId && !ids.includes(this.thing.settings.secretId)) {
          dropdown.addOption(this.thing.settings.secretId, `${this.thing.settings.secretId} (missing)`);
        }
        dropdown.setValue(this.thing.settings.secretId);
        dropdown.onChange(async (value) => {
          this.thing.settings.secretId = value;
          await this.saveConnection();
        });
      })
      .addExtraButton((button) =>
        button
          .setIcon("refresh-cw")
          .setTooltip("Reload the secret list")
          .onClick(() => this.display()),
      );

    if (this.thing.settings.secretId && !this.app.secretStorage.getSecret(this.thing.settings.secretId)) {
      containerEl.createEl("p", {
        cls: "thing-warning",
        text: "The selected secret no longer exists. Choose another secret.",
      });
    }

    new Setting(containerEl)
      .setName("Test connection")
      .setDesc("Lists the tools the server offers. It does not call an embedding provider.")
      .addButton((button) => {
        this.testButton = button.buttonEl;
        button.buttonEl.setAttr("data-thing", "test");
        button.setButtonText("Test").onClick(() => void this.testConnection());
      });

    new Setting(containerEl)
      .setName("Search panel")
      .setDesc("Open the search and related notes sidebar.")
      .addButton((button) => {
        button.buttonEl.setAttr("data-thing", "open-sidebar");
        button.setButtonText("Open").onClick(() => void this.thing.activateSidebar());
      });

    this.testStatusEl = containerEl.createEl("p", {
      cls: "thing-test-status",
      attr: { role: "status", "aria-live": "polite", "aria-atomic": "true" },
    });
  }

  override hide(): void {
    void this.thing.saveSettings();
  }

  private async saveConnection(): Promise<void> {
    await this.thing.saveSettings();
    this.thing.connectionChanged();
  }

  private async testConnection(): Promise<void> {
    if (!this.testStatusEl) return;
    this.testStatusEl.removeClass("is-error", "is-success");
    this.testStatusEl.setText("Testing the connection...");
    if (this.testButton) this.testButton.disabled = true;
    try {
      await this.thing.saveSettings();
      const tools = await this.thing.ensureTools(true);
      const related = tools.has("obsidian.related") ? "yes" : "no";
      const map = tools.has("obsidian.map") ? "yes" : "no";
      const status = tools.has("obsidian.status") ? "yes" : "no";
      this.testStatusEl.addClass("is-success");
      this.testStatusEl.setText(
        `Connected. ${tools.size} ${plural(tools.size, "tool")} available. ` +
          `Related: ${related}. Map: ${map}. Status: ${status}.`,
      );
    } catch (error) {
      this.testStatusEl.addClass("is-error");
      this.testStatusEl.setText(`Connection failed: ${errorText(error)}`);
    } finally {
      if (this.testButton) this.testButton.disabled = false;
    }
  }
}

export default ObsidianThingPlugin;
