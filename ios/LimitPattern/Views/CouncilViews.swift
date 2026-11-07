import SwiftUI

/// 多模型讨论验证：原生 SwiftUI 实现，接入「更多」页。讨论会话使用全屏 sheet，
/// 软键盘通过 KeyboardToolbar / safeAreaInset 处理，不硬塞桌面小框。
struct CouncilView: View {
    @Environment(AppState.self) private var state
    @State private var store = CouncilStore.shared
    @State private var code = ""
    @State private var date = ""
    @State private var orderOpen = false
    @State private var sourcesOpen = false
    @State private var session: JSON?
    @State private var confirmDelete: JSON?

    private var serverVersion: String { state.serverVersion }
    private var accountFingerprint: String {
        "\(state.selectedServer?.id ?? "")+\(state.authStatus["username"].string ?? state.username)"
    }

    var body: some View {
        List {
            Section {
                Text(CouncilCore.conversationDisclaimer).font(.caption).foregroundStyle(.secondary)
                    .listRowBackground(Color.clear)
                ErrorBanner(message: store.error).listRowBackground(Color.clear)
                if !store.serverSupportsCouncil {
                    Text(CouncilCore.serverUpgradeMessage).font(.callout).foregroundStyle(.orange)
                }
            }.listRowBackground(Color.clear)

            Section("发起讨论") {
                TextField("股票代码 / 名称", text: $code)
                TextField("评估日（默认最新数据日，YYYY-MM-DD）", text: $date)
                Button {
                    Task { if await store.run(code: code, date: date) { code = "" } }
                } label: {
                    Label(store.busyID == -1 ? "发起中…" : "发起讨论", systemImage: "person.3")
                }
                .disabled(!store.canStart || store.busyID == -1)
                NavigationLink { CouncilModelOrderView(store: store) } label: {
                    Label("已选模型与默认顺序（\(store.selectedOrder.count)）", systemImage: "list.number")
                }
                NavigationLink { CouncilSourcesView() } label: {
                    Label("免费公开数据源管理", systemImage: "network")
                }
            }

            Section("讨论记录") {
                if store.sessions.isEmpty && store.error == nil && store.serverSupportsCouncil {
                    Text("还没有讨论记录。").font(.caption).foregroundStyle(.secondary)
                }
                ForEach(store.sessions, id: \.councilID) { row in
                    Button { session = row } label: {
                        CouncilSessionRow(row: row, store: store)
                    }
                    .foregroundStyle(.primary)
                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                        Button(role: .destructive) { confirmDelete = row } label: { Label("删除", systemImage: "trash") }
                    }
                }
            }
        }
        .scrollContentBackground(.hidden)
        .screenBackground()
        .navigationTitle("多模型讨论验证")
        .task(id: accountFingerprint) {
            store.rebind(server: state.selectedServer?.id ?? "", account: accountFingerprint)
            await store.loadAll(serverVersion: serverVersion)
        }
        .onChange(of: state.serverText) { _, _ in
            store.rebind(server: state.selectedServer?.id ?? "", account: accountFingerprint)
            Task { await store.loadAll(serverVersion: state.serverVersion) }
        }
        .refreshable { await store.loadSessions(); await store.loadModels() }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button("终止遗留会话", systemImage: "bolt.slash") { Task { await store.reconcileStale() } }
            }
        }
        .sheet(item: $session) { row in
            CouncilSessionSheet(sessionID: row.councilID, store: store)
        }
        .confirmationDialog("删除讨论", isPresented: Binding(get: { confirmDelete != nil }, set: { if !$0 { confirmDelete = nil } }), titleVisibility: .visible) {
            if let row = confirmDelete {
                Button("永久删除", role: .destructive) { Task { _ = await store.deleteSession(id: row["id"].int ?? 0) } }
            }
            Button("取消", role: .cancel) {}
        } message: {
            if let row = confirmDelete {
                Text("确定永久删除 \(row["stock_name"].text) \(row["stock_code"].text)（\(row["eval_date"].text)）的讨论 #\(row["id"].text)？对话与证据不可恢复。")
            }
        }
    }
}

private struct CouncilSessionRow: View {
    let row: JSON
    let store: CouncilStore
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(row["stock_name"].text).font(.headline)
                Text(row["stock_code"].text).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                Spacer()
                Text(CouncilCore.conversationStatus(row)).font(.caption).foregroundStyle(row["status"].string == "failed" ? .red : (row["status"].string == "done" ? .green : .orange))
            }
            Text("\(row["eval_date"].text) · #\(row["id"].text) · \(CouncilCore.durationText(row["duration_seconds"].double))").font(.caption).foregroundStyle(.secondary)
            if let deleteError = store.detailError[row.councilID] { Text(deleteError).font(.caption).foregroundStyle(.red) }
            if let conclusion = row["conclusion"].object.isEmpty ? nil : row["conclusion"], !conclusion.isNull {
                Text("概率 \(CouncilCore.probabilityText(conclusion)) · 置信度 \(CouncilCore.confidenceLabel(conclusion["confidence"].string))").font(.caption).foregroundStyle(Theme.gold)
            }
        }
    }
}

/// 发起前选择模型与默认顺序；默认顺序保存到服务器，会话发起时按此顺序。
struct CouncilModelOrderView: View {
    let store: CouncilStore
    @State private var selected: [String] = []
    @State private var hint: String?
    @State private var task: Task<Void, Never>?

    var body: some View {
        List {
            Section {
                Text("先选择已配置且填了密钥的模型，最后一位为主持人。按上下移动调整默认顺序。").font(.caption).foregroundStyle(.secondary)
            }.listRowBackground(Color.clear)
            Section("已配置模型") {
                ForEach(store.keyModels, id: \.councilTextID) { model in
                    let id = model["id"].text
                    Button { toggle(id) } label: {
                        HStack {
                            Image(systemName: selected.contains(id) ? "checkmark.circle.fill" : "circle")
                                .foregroundStyle(selected.contains(id) ? Theme.gold : .secondary)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(model["name"].text).font(.headline)
                                Text(model["model"].text).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                            }
                            Spacer()
                        }
                    }.foregroundStyle(.primary)
                }
                if store.keyModels.isEmpty { Text("没有已填密钥的模型。").font(.caption).foregroundStyle(.secondary) }
            }
            Section("默认顺序（小值先发言）") {
                ForEach(selected, id: \.self) { id in
                    HStack {
                        Text("\(selected.firstIndex(of: id)! + 1)").font(.caption.monospacedDigit()).foregroundStyle(Theme.gold)
                        Text(displayName(id)).font(.body)
                        Spacer()
                        Button("上", systemImage: "chevron.up") { move(id, -1) }.buttonStyle(.glass)
                        Button("下", systemImage: "chevron.down") { move(id, 1) }.buttonStyle(.glass)
                    }
                }
            }
            Section {
                ErrorBanner(message: hint).listRowBackground(Color.clear)
                Button("保存默认顺序") { Task { await save() } }.buttonStyle(.glassProminent).tint(Theme.gold)
            }
        }
        .scrollContentBackground(.hidden).screenBackground()
        .navigationTitle("模型与顺序")
        .task {
            if store.keyModels.isEmpty { await store.loadModels() }
            selected = store.selectedOrder
        }
    }

    private func displayName(_ id: String) -> String {
        store.keyModels.first { $0["id"].text == id }?["name"].string ?? id
    }

    private func toggle(_ id: String) {
        if let index = selected.firstIndex(of: id) { selected.remove(at: index) } else { selected.append(id) }
    }
    private func move(_ id: String, _ dir: Int) {
        guard let index = selected.firstIndex(of: id) else { return }
        let next = index + dir
        guard selected.indices.contains(next) else { return }
        selected.swapAt(index, next)
    }
    private func save() async {
        store.order = selected
        await store.saveOrder()
        hint = store.hint
    }
}

/// 会话内群聊 + 证据 + 结论详情。
struct CouncilSessionSheet: View {
    let sessionID: Int
    let store: CouncilStore
    @Environment(\.dismiss) private var dismiss
    @State private var current: JSON
    @State private var draft = ""
    @State private var evidenceOpen = false
    @State private var conclusionOpen = false
    @State private var selectedSpeaker: String = ""
    @State private var lastComposition = Date()
    @State private var composing = false
    @State private var showBottom = false

    init(sessionID: Int, store: CouncilStore) {
        self.sessionID = sessionID
        self.store = store
        self._current = State(initialValue: .null)
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 12) {
                            ForEach(CouncilCore.chatMessages(current), id: \.messageKey) { message in
                                CouncilMessageView(message: message)
                            }
                        }
                        .padding(12)
                    }
                    .onChange(of: CouncilCore.chatMessages(current).map(\.messageKey)) { _, keys in
                        if showBottom { proxy.scrollTo(keys.last, anchor: .bottom) }
                    }
                }
                .safeAreaInset(edge: .bottom) {
                    CouncilComposer(draft: $draft, selectedSpeaker: $selectedSpeaker, composing: $composing, lastComposition: $lastComposition,
                                    models: store.keyModels, status: current["status"].string ?? "running",
                                    onSend: { submit() }, onSpeak: { speak() }, showBottom: $showBottom)
                }
            }
            .navigationTitle("\(current["stock_name"].text) \(current["stock_code"].text) #\(current["id"].text)")
            .navigationBarTitleDisplayMode(.inline)
            .safeAreaInset(edge: .top, spacing: 0) {
                if let err = store.detailError[sessionID] { ErrorBanner(message: err).padding(.horizontal) }
                if let hint = store.detailHint[sessionID] { Label(hint, systemImage: "checkmark.circle").font(.caption).foregroundStyle(.green).padding(.horizontal) }
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) { Button("关闭") { dismiss() } }
                ToolbarItem(placement: .topBarLeading) {
                    Menu {
                        Button("证据详情", systemImage: "doc.text.magnifyingglass") { evidenceOpen.toggle() }
                        Button("结论与风险", systemImage: "chart.bar") { conclusionOpen.toggle() }
                    } label: { Image(systemName: "ellipsis.circle") }
                }
            }
            .sheet(isPresented: $evidenceOpen) { CouncilEvidenceDetail(row: current) }
            .sheet(isPresented: $conclusionOpen) { CouncilConclusionDetail(row: current) }
            .onAppear { Task { await load() } }
        }
    }

    private func load() async {
        current = store.sessions.first { $0.councilID == sessionID } ?? .null
        await store.refreshDetail(id: sessionID)
        current = store.sessions.first { $0.councilID == sessionID } ?? current
    }

    private func submit() {
        let text = draft
        guard CouncilCore.shouldSend(text, isComposing: composing, sinceCompositionMs: Int(lastComposition.timeIntervalSinceNow * -1000)) else { return }
        let submitted = text.trimmingCharacters(in: .whitespacesAndNewlines)
        Task { await store.addUserMessage(id: sessionID, text: submitted) }
        draft = CouncilCore.clearSubmittedDraft(current: draft, submitted: submitted)
        showBottom = true
    }

    private func speak() {
        Task { await store.speak(id: sessionID, profileID: selectedSpeaker) }
        showBottom = true
    }
}

private struct CouncilComposer: View {
    @Binding var draft: String
    @Binding var selectedSpeaker: String
    @Binding var composing: Bool
    @Binding var lastComposition: Date
    @FocusState private var inputFocused: Bool
    let models: [JSON]
    let status: String
    let onSend: () -> Void
    let onSpeak: () -> Void
    @Binding var showBottom: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Picker("点名发言", selection: $selectedSpeaker) {
                    Text("选择模型").tag("")
                    ForEach(models, id: \.councilTextID) { model in
                        Text(model["name"].text).tag(model["id"].text)
                    }
                }.pickerStyle(.menu)
                Button("让该模型发言") { onSpeak() }.disabled(selectedSpeaker.isEmpty || status == "running")
            }
            TextEditor(text: $draft)
                .frame(minHeight: 56, maxHeight: 120)
                .padding(6)
                .glassEffect(.regular, in: .rect(cornerRadius: 12))
                .focused($inputFocused)
                .onChange(of: draft) { _, _ in lastComposition = Date() }
                .onChange(of: inputFocused) { _, focused in if focused { lastComposition = Date() } }
                .toolbar {
                    ToolbarItemGroup(placement: .keyboard) {
                        Spacer()
                        Button("完成") { inputFocused = false }
                    }
                }
            HStack {
                Text("发送仅作为用户意见写入对话；不提交未确认草稿").font(.caption2).foregroundStyle(.secondary)
                Spacer()
                Button("发送", systemImage: "arrow.up.circle.fill") { onSend() }
                    .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || composing)
            }
            if showBottom { Button("回到底部") { showBottom = false } }
        }
        .padding(10)
        .background(.ultraThinMaterial)
    }
}

private struct CouncilMessageView: View {
    let message: CouncilChatMessage
    var body: some View {
        switch message {
        case .system(let key, let name, let content, let time):
            VStack(alignment: .center, spacing: 2) {
                Text(content).font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
                if let time { Text(CouncilCore.displayTime(time)).font(.caption2).foregroundStyle(.tertiary) }
            }.frame(maxWidth: .infinity).padding(.vertical, 4)
        case .speech(let key, let kind, let content, let name, let time, let error, let model, let elapsed):
            HStack(alignment: .top, spacing: 8) {
                if kind == "user" { Spacer(minLength: 40) }
                VStack(alignment: kind == "user" ? .trailing : .leading, spacing: 4) {
                    HStack(spacing: 4) {
                        Text(name).font(.caption.bold()).foregroundStyle(kind == "user" ? Theme.gold : .primary)
                        if let model, !model.isEmpty { Text(model).font(.caption2).foregroundStyle(.secondary) }
                    }
                    CouncilSafeContent(text: content.isEmpty ? (error ?? "（无公开内容）") : content)
                        .font(.callout).textSelection(.enabled)
                        .padding(10)
                        .foregroundStyle(error != nil ? .red : .primary)
                        .glassEffect(.regular, in: .rect(cornerRadius: 12))
                        .frame(maxWidth: .infinity, alignment: .leading)
                    HStack(spacing: 6) {
                        if let time { Text(CouncilCore.displayTime(time)).font(.caption2).foregroundStyle(.tertiary) }
                        if let elapsed { Text(CouncilCore.durationText(elapsed / 1000)).font(.caption2).foregroundStyle(.tertiary) }
                        if let error { Text("失败").font(.caption2.bold()).foregroundStyle(.red) }
                    }
                }.frame(maxWidth: kind == "user" ? 320 : .infinity, alignment: kind == "user" ? .trailing : .leading)
                if kind != "user" { Spacer(minLength: 40) }
            }
        }
    }
}

private extension CouncilChatMessage {
    var messageKey: String {
        switch self {
        case .system(let key, _, _, _): return key
        case .speech(let key, _, _, _, _, _, _, _): return key
        }
    }
}

struct CouncilEvidenceDetail: View {
    let row: JSON
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    let quality = row["evidence"]["quality"]
                    if !quality.isNull {
                        GlassCard {
                            SectionTitle(title: "证据质量", systemImage: "checkmark.seal")
                            Text("实际取到 \(quality["successful_source_count"].text)/\(quality["source_count"].text) 个来源；有效空记录 \(quality["source_status_counts"]["empty"].text)；接口故障 \(quality["source_status_counts"]["failed"].text)。")
                            ForEach(quality["critical_missing"].array, id: \.self) { Text("缺项：\($0.text)") }
                        }
                    }
                    let summary = CouncilCore.evidenceSummary(row["evidence"])
                    if !summary.isEmpty { GlassCard { SectionTitle(title: "证据摘要", systemImage: "doc.text"); Text(summary) } }
                    ForEach(CouncilCore.groupedWeb(row["evidence"]), id: \.0) { category, items in
                        GlassCard {
                            SectionTitle(title: "\(CouncilCore.categoryLabel(category))（\(items.count)）", systemImage: "square.grid.2x2")
                            ForEach(items, id: \.self) { item in
                                VStack(alignment: .leading, spacing: 4) {
                                    Text("\(item["source"].text) · \(CouncilCore.sourceStatusLabel(item["status"].string))").font(.caption.bold())
                                    if let summary = item["summary"].string, !summary.isEmpty { Text(summary).font(.caption) }
                                    if item["digest"].object.count > 0 {
                                        Text(pretty(item["digest"])).font(.caption2.monospaced()).lineLimit(6)
                                    }
                                }.padding(.vertical, 4)
                            }
                        }
                    }
                }.padding()
            }
            .screenBackground()
            .navigationTitle("证据详情")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
        }
    }
    @Environment(\.dismiss) private var dismiss
    private func pretty(_ j: JSON) -> String {
        if let s = j.string { return s }
        guard let d = try? JSONSerialization.data(withJSONObject: j.any, options: [.prettyPrinted, .fragmentsAllowed]) else { return "" }
        return String(decoding: d, as: UTF8.self)
    }
}

struct CouncilConclusionDetail: View {
    let row: JSON
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    let c = row["conclusion"]
                    if c.isNull {
                        Text("暂无结论").foregroundStyle(.secondary)
                    } else {
                        GlassCard {
                            SectionTitle(title: "概率结论", systemImage: "chart.bar")
                            Text("下一交易日上涨概率：\(CouncilCore.probabilityText(c))").font(.headline)
                            Text("置信度：\(CouncilCore.confidenceLabel(c["confidence"].string))")
                            Text(CouncilCore.probabilityNotice).font(.caption).foregroundStyle(.secondary)
                        }
                        GlassCard { SectionTitle(title: "总结", systemImage: "text.quote"); Text(c["summary"].text) }
                        GlassCard { SectionTitle(title: "风险", systemImage: "exclamationmark.triangle"); Text(CouncilCore.joinList(c["risks"].array)) }
                        GlassCard { SectionTitle(title: "已核实", systemImage: "checkmark"); Text(CouncilCore.joinList(c["verified"].array)) }
                        GlassCard { SectionTitle(title: "冲突/未核实", systemImage: "questionmark"); Text(CouncilCore.joinList(c["conflicts_or_unverified"].array)) }
                        GlassCard { SectionTitle(title: "一致/分歧", systemImage: "scale.3d"); Text("一致：\(CouncilCore.joinList(c["agreements"].array))\n分歧：\(CouncilCore.joinList(c["disagreements"].array))") }
                    }
                }.padding()
            }
            .screenBackground()
            .navigationTitle("结论与风险")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
        }
    }
    @Environment(\.dismiss) private var dismiss
}

/// 来源管理：只调用服务端已审核 API，不下载代码。手动检查/应用/回滚 + 启停/优先级。
struct CouncilSourcesView: View {
    @State private var store = CouncilStore.shared
    var body: some View {
        List {
            Section {
                Text("东方财富、新浪、腾讯、同花顺、巨潮及交易所公开渠道。免费访问不代表稳定性、商业授权或覆盖完整保证。").font(.caption).foregroundStyle(.secondary)
                Text(store.sources["policy"].string ?? "仅手动检查；清单只配置已审核解析器的启停/优先级/缓存/超时，不执行代码。").font(.caption).foregroundStyle(.secondary)
            }.listRowBackground(Color.clear)
            Section("清单更新") {
                if let version = store.sources["version"].string {
                    Text("当前 \(version) · 内置 \(store.sources["builtin_version"].text) · 候选 \(store.sources["candidate_version"].text)").font(.caption)
                }
                HStack {
                    Button("检查更新") { Task { await store.sourceAction("/check-update", method: "POST") } }.disabled(store.sourceBusy)
                    if store.sources["candidate_version"].string != nil {
                        Button("应用") { Task { await store.sourceAction("/apply-update", method: "POST") } }.disabled(store.sourceBusy)
                    }
                    Button("回滚") { Task { await store.sourceAction("/rollback", method: "POST") } }.disabled(store.sourceBusy || !store.sources["rollback_available"].bool)
                }
                ErrorBanner(message: store.error).listRowBackground(Color.clear)
            }
            Section("来源") {
                ForEach(store.sources["items"].array, id: \.councilTextID) { item in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text("\(item["provider_name"].text) · \(item["category"].text)").font(.headline)
                            Spacer()
                            Toggle("", isOn: Binding(get: { item["enabled"].bool }, set: { enabled in
                                Task { await store.sourceAction("/\(item["id"].text)", method: "PUT", body: ["enabled": enabled]) }
                            })).labelsHidden().disabled(store.sourceBusy)
                        }
                        Text(item["id"].text).font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                        Text("\(CouncilCore.sourceHealthText(item)) · 优先级 \(item["priority"].text) · \(item["timeout_seconds"].text)s/\(item["cache_ttl"].text)s缓存").font(.caption)
                    }
                }
            }
        }
        .scrollContentBackground(.hidden).screenBackground()
        .navigationTitle("来源管理")
        .task { if store.sources.isNull { await store.loadSources() } }
        .refreshable { await store.loadSources() }
    }
}

private struct CouncilSafeContent: View {
    let text: String
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(CouncilCore.messageBlocks(text).enumerated()), id: \.offset) { _, block in
                switch block.kind {
                case .text:
                    Text(block.text)
                case .code(let language):
                    VStack(alignment: .leading, spacing: 2) {
                        if !language.isEmpty { Text(language).font(.caption2).foregroundStyle(.secondary) }
                        Text(block.text).font(.caption.monospaced()).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .padding(8)
                    .background(Color.black.opacity(0.25), in: RoundedRectangle(cornerRadius: 8))
                case .table:
                    ScrollView(.horizontal, showsIndicators: false) {
                        Grid(alignment: .topLeading, horizontalSpacing: 8, verticalSpacing: 8) {
                            GridRow { ForEach(block.head, id: \.self) { Text($0).font(.caption.bold()) } }
                            ForEach(Array(block.rows.enumerated()), id: \.offset) { _, row in
                                GridRow { ForEach(row, id: \.self) { Text($0).font(.caption2) } }
                            }
                        }.padding(6)
                    }
                }
            }
        }
    }
}
