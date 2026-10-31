import SwiftUI

/// 数据导出中心：网页版能导出的都在这里。
struct ExportCenterView: View {
    @State private var date = Date()
    @State private var useDate = false
    @State private var start = Calendar.current.date(byAdding: .day, value: -30, to: Date())!
    @State private var end = Date()
    @State private var sumFormat = "txt"
    @State private var includeSecrets = false
    @State private var simCode = ""
    @State private var share: ShareFile?
    @State private var error: String?
    @State private var busy: String?

    var day: String? { useDate ? DateFormatter.ymd.string(from: date) : nil }

    var body: some View {
        List {
            Section {
                Toggle("指定日期（默认最新）", isOn: $useDate)
                if useDate { DatePicker("日期", selection: $date, displayedComponents: .date) }
            } header: { Text("日期") }

            Section("给 AI 分析") {
                row("前100 · TXT（发给 AI）", "/api/ai-input", ["format": "txt", "date": day], "ai_input.txt")
                row("前100 · Markdown", "/api/ai-input", ["format": "md", "date": day], "ai_input.md")
                row("手动采集结果 · TXT", "/api/ai-input", ["snapshot": "manual", "format": "txt"], "手动采集.txt")
                row("手动采集结果 · Markdown", "/api/ai-input", ["snapshot": "manual", "format": "md"], "手动采集.md")
                row("AI 榜单 · TXT", "/api/export", ["type": "ai_ranking", "date": day], "AI榜单.txt")
            }
            Section("列表") {
                row("相似度前100（扫描）", "/api/export", ["type": "scan", "date": day, "top": "100"], "相似度前100.txt")
                HStack {
                    TextField("个股代码，例如 600132", text: $simCode).keyboardType(.numberPad)
                    Button("个股相似度") { Task { await run("/api/export", ["type": "similarity", "code": simCode, "date": day], "个股相似度.txt") } }
                        .buttonStyle(.glass).disabled(simCode.count != 6)
                }
                row("次日观察池", "/api/export", ["type": "next_day_pool", "date": day], "次日观察池.txt")
                row("涨停池", "/api/export", ["type": "pool", "date": day], "涨停池.txt")
                row("回测", "/api/export", ["type": "backtest"], "回测.txt")
                row("聚类分析", "/api/export", ["type": "cluster"], "聚类分析.txt")
                row("当日总结", "/api/export", ["type": "summary", "date": day], "每日总结.txt")
            }
            Section("自选股") {
                row("自选股 · TXT", "/api/watchlist/export", ["format": "txt"], "自选股.txt")
                row("自选股 · CSV（Excel）", "/api/watchlist/export", ["format": "csv"], "自选股.csv")
            }
            Section("每日总结 · 区间") {
                DatePicker("开始", selection: $start, displayedComponents: .date)
                DatePicker("结束", selection: $end, displayedComponents: .date)
                HStack {
                    ForEach([("近5日", -7), ("近20日", -28), ("近60日", -88)], id: \.0) { label, d in
                        Button(label) { start = Calendar.current.date(byAdding: .day, value: d, to: Date())!; end = Date() }.buttonStyle(.glass)
                    }
                }
                Picker("格式", selection: $sumFormat) { ForEach(["txt", "md", "json", "csv"], id: \.self) { Text($0.uppercased()).tag($0) } }.pickerStyle(.segmented)
                row("导出区间总结", "/api/daily-summary/export",
                    ["start": DateFormatter.ymd.string(from: start), "end": DateFormatter.ymd.string(from: end), "format": sumFormat], "每日总结.\(sumFormat)")
            }
            Section {
                Toggle("包含密钥（AI Key、企业微信 Secret 等）", isOn: $includeSecrets)
                row("导出设置配置 · JSON", "/api/settings/export", ["include_secrets": includeSecrets ? "true" : "false"], "limit-pattern-settings.json")
            } header: { Text("设置") } footer: {
                Text("包含密钥的文件请妥善保管，不要发到群里。需要服务器 0.5.2 以上。")
            }
            ErrorBanner(message: error).listRowBackground(Color.clear)
            Section { Disclaimer().listRowBackground(Color.clear) }
        }
        .scrollContentBackground(.hidden)
        .screenBackground()
        .navigationTitle("数据导出")
        .exportPreview($share)
        .task {
            if let auto = UserDefaults.standard.string(forKey: "autoexport"), share == nil {
                UserDefaults.standard.removeObject(forKey: "autoexport")
                if auto == "watchlist" { await run("/api/watchlist/export", ["format": "txt"], "自选股.txt") }
            }
        }
    }

    func row(_ title: String, _ path: String, _ q: [String: String?], _ name: String) -> some View {
        Button { Task { await run(path, q, name) } } label: {
            HStack {
                Label(title, systemImage: name.hasSuffix(".csv") ? "tablecells" : (name.hasSuffix(".json") ? "curlybraces" : "doc.text"))
                Spacer()
                if busy == name { ProgressView() } else { Image(systemName: "square.and.arrow.up").foregroundStyle(Theme.gold) }
            }
        }
        .foregroundStyle(.primary)
    }

    func run(_ path: String, _ q: [String: String?], _ name: String) async {
        busy = name; defer { busy = nil }
        error = nil
        do { share = ShareFile(url: try await APIClient.shared.download(path, query: q, fallbackName: name)) }
        catch let e as APIError where e.status == 404 && path.contains("settings/export") { error = "服务器还不支持导出设置，请升级到 0.5.2 以上" }
        catch { self.error = error.localizedDescription }
    }
}
