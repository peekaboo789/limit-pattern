import SwiftUI

struct SimilarityView: View {
    @State private var q = ""
    @State private var results: [JSON] = []
    @State private var scan: JSON = .null
    @State private var loading = false
    @State private var error: String?

    var body: some View {
        List {
            if !results.isEmpty {
                Section("搜索结果") {
                    ForEach(results, id: \.self["code"].text) { r in
                        NavigationLink { StockDetailView(item: r) } label: { HStack { Text(r["name"].text); Text(r["code"].text).foregroundStyle(.secondary) } }
                    }
                }
            }
            Section {
                Disclaimer(text: scan["disclaimer"].string ?? Theme.disclaimer).listRowBackground(Color.clear)
                if loading { ProgressView("正在扫描全市场…") }
                ErrorBanner(message: error).listRowBackground(Color.clear)
            }
            Section("最相似前 100 · \(scan["date"].text)") {
                ForEach(Array(scan["items"].array.enumerated()), id: \.offset) { i, it in
                    NavigationLink { StockDetailView(item: it) } label: { StockRow(item: it, rank: i + 1, score: it["max_similarity"]) }
                }
            }
        }
        .scrollContentBackground(.hidden).screenBackground()
        .navigationTitle("个股相似度")
        .searchable(text: $q, prompt: "代码 / 名称 / 拼音")
        .task(id: q) {
            try? await Task.sleep(for: .milliseconds(250))
            results = q.isEmpty ? [] : (((try? await APIClient.shared.get("/api/watchlist/search", ["q": q])) ?? .null)["items"].array)
        }
        .task { if scan.isNull { await load() } }
        .refreshable { await load() }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                ExportMenu(title: "导出", toolbar: true, options: [("前100 TXT", "/api/export", ["type": "scan", "date": scan["date"].string], "scan.txt"),
                                                   ("给 AI（TXT）", "/api/ai-input", ["format": "txt"], "ai_input.txt")])
            }
        }
    }
    func load() async {
        loading = true; defer { loading = false }
        do { scan = try await APIClient.shared.get("/api/scan", ["top": "100"], timeout: 300); error = nil }
        catch { self.error = error.localizedDescription }
    }
}

struct NextDayPoolView: View {
    @State private var data: JSON = .null
    @State private var error: String?
    var body: some View {
        List {
            Section {
                Text(data["formula"].string ?? "").font(.caption).foregroundStyle(.secondary)
                Text("基准涨停率 \(data["baseline_rate"].fmt(2))").font(.caption)
                Disclaimer().listRowBackground(Color.clear)
                ErrorBanner(message: error)
            }.listRowBackground(Color.clear)
            ForEach(Array(data["items"].array.enumerated()), id: \.offset) { i, it in
                NavigationLink { StockDetailView(item: it) } label: {
                    VStack(alignment: .leading) {
                        StockRow(item: it, rank: i + 1, scoreLabel: "预测分", score: it["prediction_score"], showPct: false)
                        Text("档位 \(it["band_range"].text) · 样本 \(it["band_samples"].text) · 涨停率 \(it["band_limit_up_rate"].fmt(2))").font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .scrollContentBackground(.hidden).screenBackground()
        .navigationTitle("次日观察池")
        .task { await load() }.refreshable { await load() }
        .toolbar { ToolbarItem(placement: .topBarTrailing) { ExportMenu(title: "导出", toolbar: true, options: [("TXT", "/api/export", ["type": "next_day_pool"], "next_day_pool.txt")]) } }
    }
    func load() async {
        do { data = try await APIClient.shared.get("/api/next-day-pool", ["top": "30"], timeout: 300); error = nil } catch { self.error = error.localizedDescription }
    }
}

struct SummaryListView: View {
    @State private var list: [JSON] = []
    @State private var error: String?
    @State private var start = Calendar.current.date(byAdding: .day, value: -30, to: Date())!
    @State private var end = Date()
    @State private var format = "txt"

    var body: some View {
        List {
            Section("区间导出") {
                DatePicker("开始", selection: $start, displayedComponents: .date)
                DatePicker("结束", selection: $end, displayedComponents: .date)
                Picker("格式", selection: $format) { ForEach(["txt", "md", "json", "csv"], id: \.self) { Text($0.uppercased()).tag($0) } }.pickerStyle(.segmented)
                HStack {
                    ForEach([("近5日", -7), ("近20日", -28), ("近60日", -88)], id: \.0) { label, d in
                        Button(label) { start = Calendar.current.date(byAdding: .day, value: d, to: Date())!; end = Date() }.buttonStyle(.glass)
                    }
                }
                ExportMenu(title: "导出每日总结", options: [("导出 \(format.uppercased())", "/api/daily-summary/export",
                    ["start": DateFormatter.ymd.string(from: start), "end": DateFormatter.ymd.string(from: end), "format": format], "daily_summary.\(format)")])
            }
            ErrorBanner(message: error)
            Section("已保存") {
                ForEach(Array(list.enumerated()), id: \.offset) { _, s in
                    let day = s.string ?? s["date"].text
                    NavigationLink { SummaryDetailView(day: day) } label: { VStack(alignment: .leading) { Text(day).monospacedDigit(); if let h = s["headline"].string, !h.isEmpty { Text(h).font(.caption).foregroundStyle(.secondary).lineLimit(2) } } }
                }
            }
        }
        .scrollContentBackground(.hidden).screenBackground()
        .navigationTitle("每日总结")
        .task { await load() }.refreshable { await load() }
    }
    func load() async {
        do {
            let r = try await APIClient.shared.get("/api/summaries")
            list = (r.array.isEmpty ? (r["items"].array.isEmpty ? r["dates"].array : r["items"].array) : r.array)
            error = nil
        } catch { self.error = error.localizedDescription }
    }
}

struct SummaryDetailView: View {
    let day: String
    @State private var data: JSON = .null
    @State private var error: String?
    @State private var busy = false
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                ErrorBanner(message: error)
                if let t = data["ai_text"].string ?? data["ai_summary"].string ?? data["summary_text"].string, !t.isEmpty {
                    GlassCard { SectionTitle(title: "AI 总结", systemImage: "sparkles"); Text(t).font(.callout) }
                }
                ForEach(data.object.keys.sorted().filter { !["ai_text", "ai_summary", "summary_text"].contains($0) }, id: \.self) { k in
                    GlassCard {
                        Text(k).font(.caption.bold()).foregroundStyle(Theme.gold)
                        Text(pretty(data[k])).font(.caption.monospaced()).textSelection(.enabled)
                    }
                }
            }.padding()
        }
        .screenBackground()
        .navigationTitle(day)
        .task { await load() }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button(busy ? "生成中" : "重新生成", systemImage: "arrow.clockwise") { Task { await regen() } }.disabled(busy)
            }
        }
    }
    func pretty(_ j: JSON) -> String {
        if let s = j.string { return s }
        guard let d = try? JSONSerialization.data(withJSONObject: j.any, options: [.prettyPrinted, .fragmentsAllowed]) else { return "" }
        return String(decoding: d, as: UTF8.self)
    }
    func load() async { do { data = try await APIClient.shared.get("/api/summaries/\(day)"); error = nil } catch { self.error = error.localizedDescription } }
    func regen() async {
        busy = true; defer { busy = false }
        do { data = try await APIClient.shared.post("/api/summaries/\(day)/generate", [String: Any](), timeout: 600) } catch { self.error = error.localizedDescription }
    }
}
