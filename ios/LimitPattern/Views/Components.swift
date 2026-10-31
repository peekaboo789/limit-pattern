import SwiftUI

/// 一行股票：名称代码、主分数、涨跌幅。
struct StockRow: View {
    let item: JSON
    var rank: Int? = nil
    var scoreLabel = "综合"
    var score: JSON = .null
    var showPct = true
    var body: some View {
        HStack(spacing: 12) {
            if let rank { Text("\(rank)").font(.headline.monospacedDigit()).foregroundStyle(rank <= 3 ? Theme.gold : .secondary).frame(width: 28) }
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(item["name"].text).font(.headline).lineLimit(1)
                    if item["is_limit_up"].bool { Text("涨停").font(.caption2.bold()).foregroundStyle(Theme.up) }
                    LevelBadge(level: item["level"].string ?? "")
                }
                Text("\(item["code"].text)  \(item["board_name"].string ?? "")").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 3) {
                let s = score.isNull ? (item["composite_score"].isNull ? item["max_similarity"] : item["composite_score"]) : score
                Text(s.fmt(1)).font(.title3.monospacedDigit().bold()).foregroundStyle(Theme.gold)
                if showPct {
                    let p = item["pct"].isNull ? item["change_percent"] : item["pct"]
                    PctText(value: p.double, font: .caption.monospacedDigit())
                } else { Text(scoreLabel).font(.caption2).foregroundStyle(.secondary) }
            }
        }
        .padding(.vertical, 4)
    }
}

struct KV: View {
    let k: String; let v: String; var color: Color = .primary
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(k).font(.caption2).foregroundStyle(.secondary)
            Text(v).font(.subheadline.monospacedDigit().weight(.semibold)).foregroundStyle(color).lineLimit(1).minimumScaleFactor(0.7)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct SectionTitle: View {
    let title: String; var systemImage: String = "circle.grid.2x1"
    var body: some View { Label(title, systemImage: systemImage).font(.headline).foregroundStyle(Theme.gold) }
}

/// 股票详情：把榜单 / 扫描条目里所有字段整理展示，并能加自选、看相似度。
struct StockDetailView: View {
    let item: JSON
    @State private var message: String?
    @State private var sim: JSON = .null

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                GlassCard {
                    HStack {
                        VStack(alignment: .leading) {
                            Text(item["name"].text).font(.title2.bold())
                            Text(item["code"].text).font(.subheadline.monospacedDigit()).foregroundStyle(.secondary)
                        }
                        Spacer()
                        LevelBadge(level: item["level"].string ?? "")
                    }
                    Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 10) {
                        GridRow {
                            KV(k: "综合评分", v: item["composite_score"].fmt(2), color: Theme.gold)
                            KV(k: "最高相似度", v: item["max_similarity"].fmt(1))
                            KV(k: "平均相似度", v: item["avg_similarity"].fmt(1))
                        }
                        GridRow {
                            KV(k: "外部/模型分", v: item["model_score"].fmt(1))
                            KV(k: "偏差", v: item["model_gap"].fmt(1))
                            KV(k: "预测分", v: item["prediction_score"].fmt(1))
                        }
                        GridRow {
                            KV(k: "样本数", v: item["sample_count"].text)
                            KV(k: "风险扣分", v: item["risk_penalty"].fmt(1))
                            KV(k: "连板率", v: item["carry_rate"].fmt(2))
                        }
                    }
                }
                let contrib = item["contributions"].object
                if !contrib.isEmpty {
                    GlassCard {
                        SectionTitle(title: "五项贡献", systemImage: "chart.bar.xaxis")
                        ForEach([("pattern", "形态 35"), ("technical", "技术 20"), ("volume", "量能 15"), ("capital", "资金 15"), ("history", "历史 15")], id: \.0) { key, label in
                            HStack { Text(label).font(.callout); Spacer(); Text(contrib[key]?.fmt(2) ?? "—").monospacedDigit() }
                        }
                    }
                }
                if let r = item["reason"].string, !r.isEmpty {
                    GlassCard { SectionTitle(title: "依据", systemImage: "text.quote"); Text(r).font(.callout) }
                }
                if let r = item["risk"].string, !r.isEmpty {
                    GlassCard(tint: .orange) { SectionTitle(title: "风险", systemImage: "exclamationmark.shield"); Text(r).font(.callout) }
                }
                let tags = item["tags"].array.compactMap { $0.string }
                if !tags.isEmpty { Text(tags.map { "#\($0)" }.joined(separator: "  ")).font(.caption).foregroundStyle(Theme.gold) }
                let checks = item["checks"].array
                if !checks.isEmpty {
                    GlassCard(tint: .yellow) {
                        SectionTitle(title: "⚠ 核对", systemImage: "checkmark.seal")
                        ForEach(Array(checks.enumerated()), id: \.offset) { _, c in Text(c.string ?? c["message"].string ?? c["text"].text).font(.caption) }
                    }
                }
                let matches = (sim["matches"].array.isEmpty ? item["top_matches"].array : sim["matches"].array)
                if !matches.isEmpty {
                    GlassCard {
                        SectionTitle(title: "最相似的涨停样本", systemImage: "square.stack.3d.up")
                        ForEach(Array(matches.prefix(10).enumerated()), id: \.offset) { _, m in
                            HStack {
                                Text("\(m["name"].text) \(m["code"].text)").font(.callout)
                                Text(m["limit_date"].string ?? m["date"].string ?? "").font(.caption).foregroundStyle(.secondary)
                                Spacer(); Text(m["score"].fmt(1)).monospacedDigit().foregroundStyle(Theme.gold)
                            }
                        }
                    }
                }
                ErrorBanner(message: message)
                GlassEffectContainer {
                    HStack {
                        Button("加入自选", systemImage: "star") { Task { await addWatch() } }.buttonStyle(.glass)
                        Button("相似样本", systemImage: "magnifyingglass") { Task { await loadSim() } }.buttonStyle(.glass)
                    }
                }
                Disclaimer()
            }.padding()
        }
        .screenBackground()
        .navigationTitle(item["name"].text)
        .navigationBarTitleDisplayMode(.inline)
    }

    func addWatch() async {
        do {
            let r = try await APIClient.shared.post("/api/watchlist", ["items": [["code": item["code"].text, "name": item["name"].string ?? ""]]])
            message = r["added"].array.isEmpty && (r["added"].int ?? 0) == 0 ? "已经在自选里了" : nil
            if message == nil { Notifier.hapticAlert() }
        } catch { message = error.localizedDescription }
    }
    func loadSim() async {
        do { sim = try await APIClient.shared.get("/api/similarity", ["code": item["code"].text, "top": "20"], timeout: 120) }
        catch { message = error.localizedDescription }
    }
}

/// 导出按钮：下载后弹系统分享面板（可存到「文件」）。
struct ExportMenu: View {
    let title: String
    let options: [(String, String, [String: String?], String)] // (label, path, query, fallback filename)
    @State private var share: ShareFile?
    @State private var error: String?
    @State private var busy = false
    var body: some View {
        Menu {
            ForEach(options.indices, id: \.self) { i in
                Button(options[i].0) { Task { await run(options[i]) } }
            }
        } label: { Label(busy ? "导出中…" : title, systemImage: "square.and.arrow.up") }
        .buttonStyle(.glass)
        .shareSheet($share)
        .alert("导出失败", isPresented: .constant(error != nil)) { Button("好") { error = nil } } message: { Text(error ?? "") }
    }
    func run(_ o: (String, String, [String: String?], String)) async {
        busy = true; defer { busy = false }
        do { share = ShareFile(url: try await APIClient.shared.download(o.1, query: o.2, fallbackName: o.3)) }
        catch { self.error = error.localizedDescription }
    }
}
