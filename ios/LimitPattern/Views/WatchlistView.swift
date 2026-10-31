import SwiftUI
import UniformTypeIdentifiers

struct WatchlistView: View {
    @State private var data: JSON = .null
    @State private var notes: [String: String] = [:]
    @State private var error: String?
    @State private var showAdd = false
    @State private var showImport = false
    @State private var editNote: JSON?
    @State private var noteText = ""
    @State private var editMode: EditMode = .inactive
    @State private var alerted: Set<String> = []

    var items: [JSON] { data["items"].array }
    var active: Bool { data["alerts_active"].bool }

    var body: some View {
        List {
            Section {
                HStack {
                    Text(data["status_text"].string ?? "自选股").font(.subheadline.bold())
                    Spacer()
                    Text(data["price_label"].string ?? "").font(.caption).foregroundStyle(.secondary)
                }
                .listRowBackground(Color.clear)
                ErrorBanner(message: error).listRowBackground(Color.clear)
            }
            ForEach(items, id: \.self["code"].text) { it in
                let lv = it["level"].string ?? "normal"
                VStack(alignment: .leading, spacing: 4) {
                    StockRow(item: it, score: it["composite_score"])
                    HStack(spacing: 10) {
                        Text("现价 \(it["price"].fmt())")
                        Text("高 \(it["high"].fmt())").foregroundStyle(Theme.up.opacity(0.8))
                        Text("低 \(it["low"].fmt())").foregroundStyle(Theme.down.opacity(0.8))
                        Text("量比 \(it["volume_ratio"].fmt(2))")
                        Text("相似 \(it["max_similarity"].fmt(1))")
                    }.font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                    if let n = notes[it["code"].text], !n.isEmpty { Text("📝 \(n)").font(.caption).foregroundStyle(Theme.gold) }
                }
                .padding(8)
                .background { if active && lv == "alert" { RoundedRectangle(cornerRadius: 14).stroke(Theme.up, lineWidth: 2) } }
                .listRowBackground(Color.clear)
                .swipeActions {
                    Button("删除", systemImage: "trash", role: .destructive) { Task { await delete([it["code"].text]) } }
                    Button("备注", systemImage: "note.text") { noteText = notes[it["code"].text] ?? ""; editNote = it }.tint(.orange)
                }
            }
            .onMove { from, to in
                var codes = items.map { $0["code"].text }
                codes.move(fromOffsets: from, toOffset: to)
                Task { await reorder(codes) }
            }
        }
        .environment(\.editMode, $editMode)
        .scrollContentBackground(.hidden)
        .screenBackground()
        .navigationTitle("自选股")
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Button(editMode == .active ? "完成" : "排序") { editMode = editMode == .active ? .inactive : .active }
            }
            ToolbarItemGroup(placement: .topBarTrailing) {
                Button("添加", systemImage: "plus") { showAdd = true }
                Menu {
                    Button("批量导入", systemImage: "square.and.arrow.down") { showImport = true }
                    Button("加入 AI 榜单全部", systemImage: "list.number") { Task { await addFromRanking() } }
                    Button("加入手动采集前 20", systemImage: "bolt") { Task { await addFromSnapshot() } }
                    Button("清空自选", systemImage: "trash", role: .destructive) { Task { await clearAll() } }
                } label: { Label("更多", systemImage: "ellipsis") }
            }
            ToolbarItem(placement: .topBarTrailing) {
                ExportMenu(title: "导出自选", toolbar: true, options: [
                    ("TXT", "/api/watchlist/export", ["format": "txt"], "watchlist.txt"),
                    ("CSV", "/api/watchlist/export", ["format": "csv"], "watchlist.csv"),
                ])
            }
        }
        .task {
            while !Task.isCancelled {
                await load()
                try? await Task.sleep(for: .seconds(active ? 5 : 60))
            }
        }
        .refreshable { await load() }
        .sheet(isPresented: $showAdd) { AddStockSheet { Task { await load() } } }
        .sheet(isPresented: $showImport) { ImportWatchSheet { Task { await load() } } }
        .alert("备注", isPresented: Binding(get: { editNote != nil }, set: { if !$0 { editNote = nil } })) {
            TextField("备注", text: $noteText)
            Button("保存") { if let it = editNote { Task { await saveNote(it["code"].text, noteText) } } }
            Button("取消", role: .cancel) {}
        }
    }

    func load() async {
        do {
            data = try await APIClient.shared.get("/api/watchlist/monitor", timeout: 20); error = nil
            if let wl = try? await APIClient.shared.get("/api/watchlist") {
                notes = Dictionary(wl["items"].array.map { ($0["code"].text, $0["note"].string ?? "") }, uniquingKeysWith: { a, _ in a })
            }
            if active { for it in items where it["level"].string == "alert" {
                let k = "\(data["date"].text)-\(it["code"].text)"; if !alerted.contains(k) { alerted.insert(k); Notifier.hapticAlert() } } }
        } catch { self.error = error.localizedDescription }
    }
    func delete(_ codes: [String]) async { _ = try? await APIClient.shared.post("/api/watchlist/delete", ["codes": codes]); await load() }
    func clearAll() async { _ = try? await APIClient.shared.post("/api/watchlist/delete", ["all": true]); await load() }
    func reorder(_ codes: [String]) async { _ = try? await APIClient.shared.post("/api/watchlist/reorder", ["codes": codes]); await load() }
    func saveNote(_ code: String, _ note: String) async { _ = try? await APIClient.shared.post("/api/watchlist/note", ["code": code, "note": note]); await load() }
    func addCodes(_ codes: [String]) async {
        guard !codes.isEmpty else { error = "没有可加入的股票"; return }
        do { _ = try await APIClient.shared.post("/api/watchlist", ["codes": codes]) } catch { self.error = error.localizedDescription }
        await load()
    }
    func addFromRanking() async {
        let r = (try? await APIClient.shared.get("/api/ai-ranking")) ?? .null
        await addCodes(r["items"].array.map { $0["code"].text })
    }
    func addFromSnapshot() async {
        let r = (try? await APIClient.shared.get("/api/snapshot/manual")) ?? .null
        await addCodes(r["result"]["items"].array.prefix(20).map { $0["code"].text })
    }
}

struct AddStockSheet: View {
    var onDone: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var q = ""
    @State private var results: [JSON] = []
    @State private var msg: String?
    var body: some View {
        NavigationStack {
            List {
                if let msg { Text(msg).font(.caption) }
                ForEach(results, id: \.self["code"].text) { r in
                    Button {
                        Task {
                            let res = try? await APIClient.shared.post("/api/watchlist", ["items": [["code": r["code"].text, "name": r["name"].string ?? ""]]])
                            msg = (res?["duplicates"].array.isEmpty ?? true) ? "已加入 \(r["name"].text)" : "\(r["name"].text) 已经在自选里"
                            onDone()
                        }
                    } label: {
                        HStack { Text(r["name"].text); Text(r["code"].text).foregroundStyle(.secondary).monospacedDigit(); Spacer(); Image(systemName: "plus.circle") }
                    }
                }
            }
            .searchable(text: $q, placement: .navigationBarDrawer(displayMode: .always), prompt: "代码 / 名称 / 拼音首字母")
            .task(id: q) {
                try? await Task.sleep(for: .milliseconds(250))
                guard !q.trimmingCharacters(in: .whitespaces).isEmpty else { results = []; return }
                results = ((try? await APIClient.shared.get("/api/watchlist/search", ["q": q, "limit": "20"])) ?? .null)["items"].array
            }
            .navigationTitle("添加自选")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成", systemImage: "checkmark") { dismiss() } } }
        }
    }
}

struct ImportWatchSheet: View {
    var onDone: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var preview: JSON = .null
    @State private var chosen: [String: String] = [:] // token -> code
    @State private var picking = false
    @State private var msg: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("粘贴代码或名称（同花顺 / 东方财富导出也行）") {
                    TextEditor(text: $text).frame(minHeight: 140)
                    HStack {
                        Button("粘贴", systemImage: "doc.on.clipboard") { text = UIPasteboard.general.string ?? text }
                        Spacer()
                        Button("选文件", systemImage: "folder") { picking = true }
                        Spacer()
                        Button("预览", systemImage: "eye") { Task { await doPreview() } }.disabled(text.isEmpty)
                    }.buttonStyle(.glass)
                }
                if !preview.isNull {
                    Section("已匹配 \(preview["matched"].array.count)") {
                        ForEach(preview["matched"].array, id: \.self["code"].text) { m in Text("\(m["name"].text) \(m["code"].text)") }
                    }
                    if !preview["ambiguous"].array.isEmpty {
                        Section("有多个候选，选一个") {
                            ForEach(preview["ambiguous"].array, id: \.self["token"].text) { a in
                                Picker(a["token"].text, selection: Binding(get: { chosen[a["token"].text] ?? "" }, set: { chosen[a["token"].text] = $0 })) {
                                    Text("跳过").tag("")
                                    ForEach(a["candidates"].array, id: \.self["code"].text) { c in Text("\(c["name"].text) \(c["code"].text)").tag(c["code"].text) }
                                }
                            }
                        }
                    }
                    if !preview["not_found"].array.isEmpty { Section("找不到") { Text(preview["not_found"].array.map { $0.string ?? $0["token"].text }.joined(separator: "、")) } }
                    if !preview["duplicates"].array.isEmpty { Section("已存在") { Text(preview["duplicates"].array.map { $0.string ?? $0["code"].text }.joined(separator: "、")) } }
                }
                if let msg { Section { Text(msg) } }
            }
            .navigationTitle("批量导入")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("关闭", systemImage: "xmark") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("加入") { Task { await commit() } }.buttonStyle(.glassProminent).disabled(preview.isNull) }
            }
            .fileImporter(isPresented: $picking, allowedContentTypes: [.plainText, .commaSeparatedText, .json, .text]) { r in
                if case .success(let url) = r {
                    let ok = url.startAccessingSecurityScopedResource(); defer { if ok { url.stopAccessingSecurityScopedResource() } }
                    if let d = try? Data(contentsOf: url) {
                        text = String(data: d, encoding: .utf8) ?? String(data: d, encoding: String.Encoding(rawValue: CFStringConvertEncodingToNSStringEncoding(CFStringEncoding(CFStringEncodings.GB_18030_2000.rawValue)))) ?? ""
                    }
                }
            }
        }
    }
    func doPreview() async {
        do { preview = try await APIClient.shared.post("/api/watchlist/import/preview", ["text": text]); msg = nil }
        catch { msg = error.localizedDescription }
    }
    func commit() async {
        var items: [[String: String]] = preview["matched"].array.map { ["code": $0["code"].text, "name": $0["name"].string ?? ""] }
        items += chosen.values.filter { !$0.isEmpty }.map { ["code": $0] }
        guard !items.isEmpty else { msg = "没有可加入的股票"; return }
        do {
            let r = try await APIClient.shared.post("/api/watchlist", ["items": items])
            msg = "已加入 \(r["added"].array.count) 只"; onDone()
        } catch { msg = error.localizedDescription }
    }
}
