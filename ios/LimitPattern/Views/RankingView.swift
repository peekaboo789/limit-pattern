import SwiftUI
import UniformTypeIdentifiers

struct RankingView: View {
    @State private var data: JSON = .null
    @State private var dates: [String] = []
    @State private var date: String = ""
    @State private var sortBySystem = false
    @State private var error: String?
    @State private var generating = false
    @State private var progress: JSON = .null
    @State private var showImport = false

    var items: [JSON] {
        let list = data["items"].array
        return sortBySystem ? list.sorted { ($0["composite_score"].double ?? 0) > ($1["composite_score"].double ?? 0) } : list
    }
    var external: Bool { data["trigger"].string == "external" || data["order"].string == "external" }

    var body: some View {
        List {
            Section {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Text(data["date"].string.map { "\($0) 榜单" } ?? "还没有榜单").font(.headline)
                        Spacer()
                        Text(external ? "外部 AI" : (data["model"].string ?? "")).font(.caption).foregroundStyle(.secondary)
                    }
                    let meta = data["external_meta"]
                    if let t = meta["title"].string, !t.isEmpty { Text(t).font(.subheadline.bold()) }
                    if let s = meta["market_sentiment"].string, !s.isEmpty { Text(s).font(.caption) }
                    if let e = data["ai_error"].string, !e.isEmpty { Text("AI：\(e)").font(.caption).foregroundStyle(.orange) }
                    if generating {
                        ProgressView(value: progressValue) { Text(progress["message"].string ?? "正在生成…").font(.caption) }
                    }
                    Picker("排序", selection: $sortBySystem) {
                        Text(external ? "按外部 AI" : "榜单顺序").tag(false)
                        Text("按系统综合分").tag(true)
                    }.pickerStyle(.segmented)
                    Disclaimer(text: data["disclaimer"].string ?? Theme.disclaimer)
                }
                .padding(12).glassEffect(.regular, in: .rect(cornerRadius: 20))
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }
            ErrorBanner(message: error).listRowBackground(Color.clear)
            ForEach(Array(items.enumerated()), id: \.offset) { i, it in
                NavigationLink { StockDetailView(item: it) } label: {
                    VStack(alignment: .leading, spacing: 4) {
                        StockRow(item: it, rank: i + 1, scoreLabel: external ? "外部分" : "综合",
                                 score: external && !sortBySystem ? it["model_score"] : it["composite_score"], showPct: false)
                        HStack(spacing: 10) {
                            if external { Text("系统 \(it["composite_score"].fmt(1)) · 差 \(it["model_gap"].fmt(1))").font(.caption2).foregroundStyle(.secondary) }
                            if !it["priority"].isNull { Text("优先级 \(it["priority"].text)").font(.caption2).foregroundStyle(.secondary) }
                            if !it["checks"].array.isEmpty { Text("⚠ 核对").font(.caption2).foregroundStyle(.yellow) }
                        }
                        if let r = it["reason"].string, !r.isEmpty { Text(r).font(.caption).foregroundStyle(.secondary).lineLimit(2) }
                    }
                }
                .listRowBackground(Color.clear)
            }
        }
        .scrollContentBackground(.hidden)
        .screenBackground()
        .navigationTitle("AI 榜单")
        .refreshable { await load() }
        .task { await loadDates(); await load() }
        .toolbar {
            ToolbarItem(placement: .topBarLeading) { ModelMenu() }
            ToolbarItem(placement: .topBarLeading) {
                Menu {
                    Picker("日期", selection: $date) { ForEach(dates, id: \.self) { Text($0).tag($0) } }
                } label: { Label(date.isEmpty ? "日期" : date, systemImage: "calendar") }
            }
            ToolbarItemGroup(placement: .topBarTrailing) {
                Button("导入", systemImage: "square.and.arrow.down") { showImport = true }
                Menu {
                    Button("生成（最新行情）", systemImage: "sparkles") { Task { await generate([:]) } }
                    Button("用手动采集快照生成", systemImage: "bolt") { Task { await generate(["snapshot": "manual"]) } }
                    Button("用 11:30 冻结快照生成", systemImage: "clock") { Task { await generate(["snapshot": "morning"]) } }
                } label: { Label("生成", systemImage: "sparkles") }
                .disabled(generating)
            }
            ToolbarItem(placement: .topBarTrailing) {
                ExportMenu(title: "导出", toolbar: true, options: [
                    ("榜单 TXT", "/api/export", ["type": "ai_ranking", "date": data["date"].string], "ai_ranking.txt"),
                    ("前100给 AI（TXT）", "/api/ai-input", ["format": "txt"], "ai_input.txt"),
                    ("前100给 AI（MD）", "/api/ai-input", ["format": "md"], "ai_input.md"),
                ])
            }
        }
        .onChange(of: date) { _, _ in Task { await load() } }
        .modelToast()
        .sheet(isPresented: $showImport) { ImportRankingSheet(defaultDate: data["date"].string ?? "") { Task { await loadDates(); await load() } } }
    }

    var progressValue: Double {
        let d = progress["done"].double ?? 0, t = progress["total"].double ?? 0
        return t > 0 ? min(d / t, 1) : 0
    }
    func loadDates() async {
        if let d = try? await APIClient.shared.get("/api/ai-ranking/dates") { dates = d["dates"].array.compactMap { $0.string } }
    }
    func load() async {
        do { data = try await APIClient.shared.get("/api/ai-ranking", ["date": date.isEmpty ? nil : date]); error = nil }
        catch let e as APIError where e.status == 404 { data = .null; error = "还没有 AI 榜单，点右上角生成或导入" }
        catch { self.error = error.localizedDescription }
    }
    func generate(_ body: [String: Any]) async {
        generating = true
        let poll = Task { while !Task.isCancelled { if let p = try? await APIClient.shared.get("/api/ai-ranking/progress") { progress = p }; try? await Task.sleep(for: .seconds(2)) } }
        defer { poll.cancel(); generating = false }
        do {
            data = try await APIClient.shared.post("/api/ai-ranking/generate", body, timeout: 900)
            date = ""; error = nil
            await loadDates()
        } catch { self.error = error.localizedDescription }
    }
}

struct ImportRankingSheet: View {
    let defaultDate: String
    var onDone: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var model = ""
    @State private var date = ""
    @State private var picking = false
    @State private var result: String?
    @State private var busy = false

    var body: some View {
        NavigationStack {
            Form {
                Section("外部 AI 返回的 JSON") {
                    TextEditor(text: $text).frame(minHeight: 220).font(.caption.monospaced())
                    HStack {
                        Button("粘贴", systemImage: "doc.on.clipboard") { text = UIPasteboard.general.string ?? text }
                        Spacer()
                        Button("选文件", systemImage: "folder") { picking = true }
                    }.buttonStyle(.glass)
                }
                Section("可选") {
                    TextField("日期（留空用 JSON 里的 date）", text: $date)
                    TextField("模型名，例如 DeepSeek", text: $model)
                }
                if let result { Section { Text(result).font(.callout) } }
            }
            .navigationTitle("导入外部 AI 结果")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("关闭", systemImage: "xmark") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(busy ? "导入中" : "导入") { Task { await submit() } }.buttonStyle(.glassProminent).disabled(text.isEmpty || busy)
                }
            }
            .fileImporter(isPresented: $picking, allowedContentTypes: [.json, .plainText, .text]) { r in
                if case .success(let url) = r {
                    let ok = url.startAccessingSecurityScopedResource(); defer { if ok { url.stopAccessingSecurityScopedResource() } }
                    if let d = try? Data(contentsOf: url) { text = String(decoding: d, as: UTF8.self) }
                }
            }
        }
    }
    func submit() async {
        busy = true; defer { busy = false }
        var body: [String: Any] = ["text": text]
        if !date.isEmpty { body["date"] = date }
        if !model.isEmpty { body["model"] = model }
        do {
            let r = try await APIClient.shared.post("/api/ai-ranking/import", body, timeout: 300)
            var msg = "已替换 \(r["date"].text) 榜单：\(r["items"].array.count) 只"
            let rej = r["rejected"].array
            if !rej.isEmpty { msg += "\n未导入：" + rej.map { "\($0["code"].text) \($0["reason"].text)" }.joined(separator: "；") }
            let warn = r["name_warnings"].array
            if !warn.isEmpty { msg += "\n名称提示：" + warn.map { $0.string ?? $0["message"].text }.joined(separator: "；") }
            result = msg
            onDone()
        } catch { result = "导入失败：\(error.localizedDescription)" }
    }
}
