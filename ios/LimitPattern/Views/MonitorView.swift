import SwiftUI

struct MonitorView: View {
    @State private var data: JSON = .null
    @State private var error: String?
    @State private var alerted: Set<String> = []

    var active: Bool { data["alerts_active"].isNull ? data["market_open"].bool : data["alerts_active"].bool }

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                GlassCard {
                    HStack {
                        SectionTitle(title: data["status_text"].string ?? "实时监控", systemImage: active ? "dot.radiowaves.left.and.right" : "moon.zzz")
                        Spacer()
                        Text(data["updated_at"].string.map { String($0.suffix(8)) } ?? "").font(.caption.monospaced()).foregroundStyle(.secondary)
                    }
                    Text("监控 \(data["ranking_date"].text) 的榜单 · 涨超 \(data["thresholds"]["warn"].fmt(0))% 提醒，涨超 \(data["thresholds"]["alert"].fmt(0))% 红框" + (active ? "" : "（休市，报警暂停）"))
                        .font(.caption).foregroundStyle(.secondary)
                    if let e = data["error"].string, !e.isEmpty { Text(e).font(.caption).foregroundStyle(.orange) }
                }
                ErrorBanner(message: error)
                ForEach(Array(data["items"].array.enumerated()), id: \.offset) { _, it in
                    let lv = it["level"].string ?? "normal"
                    GlassCard(tint: active && lv == "warn" ? .orange : nil, alert: active && lv == "alert") {
                        StockRow(item: it, rank: it["rank"].int, score: it["ai_score"])
                        HStack {
                            Text("现价 \(it["price"].fmt())").font(.caption.monospacedDigit())
                            Text("昨收 \(it["pre_close"].fmt())").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                        }
                    }
                    .opacity(active ? 1 : 0.75)
                }
            }.padding()
        }
        .screenBackground()
        .navigationTitle("实时监控")
        .task {
            while !Task.isCancelled {
                await load()
                try? await Task.sleep(for: .seconds(active ? 5 : 60))
            }
        }
        .refreshable { await load() }
    }

    func load() async {
        do {
            data = try await APIClient.shared.get("/api/monitor", timeout: 20); error = nil
            guard active else { return }
            for it in data["items"].array where it["level"].string == "alert" {
                let key = "\(data["date"].text)-\(it["code"].text)"
                if !alerted.contains(key) { alerted.insert(key); Notifier.hapticAlert() }
            }
        } catch { self.error = error.localizedDescription }
    }
}
