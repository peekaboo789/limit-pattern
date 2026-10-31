import SwiftUI

@MainActor
@Observable
final class CaptureModel {
    static let shared = CaptureModel()
    var status: JSON = .null
    var snapshot: JSON = .null
    var error: String?
    var fallbackHint: String?
    var running: Bool { status["running"].bool }

    func load() async {
        snapshot = (try? await APIClient.shared.get("/api/snapshot/manual")) ?? snapshot
        if let s = try? await APIClient.shared.get("/api/snapshot/capture/status") {
            status = s
            if s["running"].bool { await poll() }
        }
    }
    func start() async {
        error = nil; fallbackHint = nil
        do {
            status = try await APIClient.shared.post("/api/snapshot/capture", [String: Any]())
            await poll()
        } catch let e as APIError where e.status == 409 {
            fallbackHint = e.message
        } catch { self.error = error.localizedDescription }
    }
    func fallback() async {
        fallbackHint = nil
        do {
            _ = try await APIClient.shared.post("/api/snapshot/capture", ["fallback": true], timeout: 300)
            snapshot = (try? await APIClient.shared.get("/api/snapshot/manual")) ?? snapshot
        } catch { self.error = error.localizedDescription }
    }
    func cancel() async { _ = try? await APIClient.shared.post("/api/snapshot/capture/cancel") }
    func poll() async {
        while true {
            guard let s = try? await APIClient.shared.get("/api/snapshot/capture/status") else { break }
            status = s
            if !s["running"].bool { break }
            try? await Task.sleep(for: .seconds(1))
        }
        if let e = status["error"].string, !e.isEmpty { error = e }
        snapshot = (try? await APIClient.shared.get("/api/snapshot/manual")) ?? snapshot
    }
}

struct OverviewView: View {
    @Binding var tab: Int
    @Environment(AppState.self) private var state
    @State private var capture = CaptureModel.shared
    @State private var monitor: JSON = .null
    @State private var sync: JSON = .null
    @State private var genMsg: String?
    @State private var generating = false
    @State private var showTop100 = false

    var body: some View {
        ScrollView {
            VStack(spacing: 14) {
                serverCard
                captureCard
                monitorCard
                Disclaimer()
            }.padding()
        }
        .screenBackground()
        .navigationTitle("总览")
        .refreshable { await reload() }
        .task { await reload() }
        .navigationDestination(isPresented: $showTop100) { SnapshotListView(snapshot: capture.snapshot) }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button("同步行情", systemImage: "arrow.triangle.2.circlepath") { Task { await startSync() } }
            }
        }
    }

    var serverCard: some View {
        GlassCard {
            HStack { SectionTitle(title: "服务器", systemImage: "server.rack"); Spacer(); Text("v\(state.serverVersion)").font(.caption.monospaced()).foregroundStyle(.secondary) }
            Grid(alignment: .leading) {
                GridRow {
                    KV(k: "最新行情", v: state.health["data_date"].text)
                    KV(k: "保存天数", v: state.health["stored_days"].text)
                    KV(k: "数据源", v: state.health["active_source"].string.flatMap { $0.isEmpty ? nil : $0 } ?? state.health["data_source"].text)
                }
            }
            if sync["running"].bool {
                ProgressView(value: (sync["progress"].double ?? 0) / 100) { Text(sync["message"].text).font(.caption) }
            } else if let m = sync["message"].string, !m.isEmpty {
                Text(m).font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    var captureCard: some View {
        GlassCard(tint: Theme.gold) {
            SectionTitle(title: "手动采集当日行情", systemImage: "bolt.fill")
            Text("抓取截至此刻的全市场行情，和近 30 天涨停样本比较，选出前 100。").font(.caption).foregroundStyle(.secondary)
            if capture.running {
                let done = capture.status["done"].double ?? 0, total = max(capture.status["total"].double ?? 0, 1)
                ProgressView(value: min(done / total, 1)) { Text(capture.status["message"].text).font(.caption) }
                Button("取消", role: .cancel) { Task { await capture.cancel() } }.buttonStyle(.glass)
            } else {
                Button { Task { await capture.start() } } label: {
                    Label("采集当前行情并选前100", systemImage: "bolt.horizontal.fill").frame(maxWidth: .infinity).bold()
                }.buttonStyle(.glassProminent).tint(Theme.gold)
                Button { Task { await captureAndAnalyze() } } label: {
                    Label(generating ? "AI 分析中…" : "一键采集 + AI 分析", systemImage: "sparkles").frame(maxWidth: .infinity)
                }.buttonStyle(.glass).disabled(generating)
            }
            if let h = capture.fallbackHint {
                Text(h).font(.caption)
                Button("改用上一交易日数据") { Task { await capture.fallback() } }.buttonStyle(.glass)
            }
            ErrorBanner(message: capture.error)
            let snap = capture.snapshot
            if !snap.isNull {
                Divider()
                HStack {
                    Text(snap["label"].string ?? "快照 \(snap["date"].text)").font(.subheadline.bold())
                    if snap["superseded"].bool { Text("已被正式数据替换").font(.caption2).foregroundStyle(.secondary) }
                }
                Text("截至 \(snap["taken_at"].text) · 样本池 \(snap["result"]["pool_size"].text) · 用时 \(snap["result"]["elapsed_seconds"].fmt(1)) 秒")
                    .font(.caption).foregroundStyle(.secondary)
                ForEach(Array(snap["result"]["items"].array.prefix(5).enumerated()), id: \.offset) { i, it in
                    NavigationLink { StockDetailView(item: it) } label: { StockRow(item: it, rank: i + 1) }.buttonStyle(.plain)
                }
                GlassEffectContainer(spacing: 8) {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack {
                            Button("完整前 100", systemImage: "list.bullet") { showTop100 = true }.buttonStyle(.glass)
                            ExportMenu(title: "导出给 AI", options: [
                                ("TXT", "/api/ai-input", ["snapshot": "manual", "format": "txt"], "ai_input_manual.txt"),
                                ("Markdown", "/api/ai-input", ["snapshot": "manual", "format": "md"], "ai_input_manual.md"),
                            ])
                            Button(generating ? "生成中…" : "生成 AI 榜单", systemImage: "sparkles") { Task { await generate() } }
                                .buttonStyle(.glass).disabled(generating)
                        }
                    }
                }
                if let genMsg { Text(genMsg).font(.caption).foregroundStyle(.secondary) }
            }
        }
    }

    var monitorCard: some View {
        GlassCard(alert: monitor["alerts_active"].bool && (monitor["alert_count"].int ?? 0) > 0) {
            HStack {
                SectionTitle(title: "AI 榜单 · 实时监控", systemImage: "waveform.path.ecg")
                Spacer()
                Text(monitor["status_text"].text).font(.caption).foregroundStyle(monitor["market_open"].bool ? Theme.up : .secondary)
            }
            if let e = monitor["error"].string, !e.isEmpty { Text(e).font(.caption).foregroundStyle(.secondary) }
            ForEach(Array(monitor["items"].array.prefix(8).enumerated()), id: \.offset) { _, it in
                StockRow(item: it, rank: it["rank"].int, score: it["ai_score"])
            }
            Button("查看全部", systemImage: "arrow.right") { tab = 2 }.buttonStyle(.glass)
        }
    }

    func reload() async {
        await state.refreshHealth()
        monitor = (try? await APIClient.shared.get("/api/monitor")) ?? monitor
        sync = (try? await APIClient.shared.get("/api/sync/status")) ?? sync
        await capture.load()
    }
    func startSync() async {
        _ = try? await APIClient.shared.post("/api/sync")
        while true {
            guard let s = try? await APIClient.shared.get("/api/sync/status") else { break }
            sync = s
            if !s["running"].bool { break }
            try? await Task.sleep(for: .seconds(2))
        }
        await state.refreshHealth()
    }
    /// 采集当前行情 → 用这份快照生成 AI 榜单。
    func captureAndAnalyze() async {
        genMsg = "第 1 步：采集当前行情…"
        await capture.start()
        if capture.fallbackHint != nil { genMsg = "现在不能采集，先选「改用上一交易日数据」再点生成 AI 榜单"; return }
        if capture.error != nil || capture.status["phase"].string == "cancelled" { genMsg = "采集没完成，已停止"; return }
        genMsg = "第 2 步：AI 分析…"
        await generate()
    }
    func generate() async {
        generating = true; defer { generating = false }
        genMsg = "正在用 \(ModelStore.shared.activeName.isEmpty ? "当前模型" : ModelStore.shared.activeName) 生成，可能要几分钟…"
        do {
            let r = try await APIClient.shared.post("/api/ai-ranking/generate", ["snapshot": "manual"], timeout: 900)
            genMsg = "已生成 \(r["date"].text) 榜单：\(r["items"].array.count) 只" + ((r["ai_error"].string ?? "").isEmpty ? "" : "（\(r["ai_error"].text)）")
        } catch { genMsg = error.localizedDescription }
    }
}

struct SnapshotListView: View {
    let snapshot: JSON
    var body: some View {
        List {
            Section { Disclaimer().listRowBackground(Color.clear) }
            ForEach(Array(snapshot["result"]["items"].array.enumerated()), id: \.offset) { i, it in
                NavigationLink { StockDetailView(item: it) } label: { StockRow(item: it, rank: i + 1) }
                    .listRowBackground(Color.clear)
            }
        }
        .scrollContentBackground(.hidden)
        .screenBackground()
        .navigationTitle(snapshot["label"].string ?? "手动采集前 100")
    }
}
