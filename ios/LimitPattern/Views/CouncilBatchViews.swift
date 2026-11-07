import SwiftUI
import UniformTypeIdentifiers

private struct BatchSessionLink: Identifiable { let id: Int }

/// 批量 TXT 导入与批次列表。原生入口，复用单股 CouncilSessionSheet 查看已有会话。
struct CouncilBatchView: View {
    @Environment(AppState.self) private var state
    @State private var store = CouncilBatchStore.shared
    @State private var councilStore = CouncilStore.shared
    @State private var importerOpen = false
    @State private var selectedCodes = Set<String>()
    @State private var confirmStart = false
    @State private var sessionLink: BatchSessionLink?

    private var serverVersion: String { state.serverVersion }
    private var accountFingerprint: String {
        "\(state.selectedServer?.id ?? "")+\(state.authStatus["username"].string ?? state.username)"
    }

    var body: some View {
        List {
            Section {
                Text("一次导入一张 UTF-8/BOM 的 TSV/TXT 排名表；预览后勾选股票、评估日和至少两个已配置有密钥的模型，确认后才开始。单股按现有讨论队列串行执行，不会并发调用模型。")
                    .font(.caption).foregroundStyle(.secondary)
                if !store.serverSupportsBatch { Text(CouncilBatchCore.batchUpgradeMessage).font(.callout).foregroundStyle(.orange) }
                ErrorBanner(message: store.lastError).listRowBackground(Color.clear)
            }.listRowBackground(Color.clear)

            Section("TXT 导入") {
                Button { importerOpen = true } label: { Label("选择 TXT / TSV 文件", systemImage: "doc.badge.plus") }
                    .disabled(store.previewLoading || !store.serverSupportsBatch)
                if store.previewLoading { ProgressView() }
                if !store.preview.isNull {
                    Text("文件 \(store.preview["source_name"].text) · 数据日期 \(store.preview["source_date"].text)")
                        .font(.caption)
                    Text("有效候选 \(store.preview["counts"]["valid"].text)，无效 \(store.preview["counts"]["invalid"].text)，共 \(store.preview["counts"]["total"].text)")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }

            if !store.preview.isNull {
                Section("候选股票（已选 \(selectedCodes.count)）") {
                    ForEach(store.preview["items"].array, id: \.councilTextID) { item in
                        let code = item["code"].text
                        HStack {
                            Image(systemName: selectedCodes.contains(code) ? "checkmark.circle.fill" : "circle")
                                .foregroundStyle(selectedCodes.contains(code) ? Theme.gold : .secondary)
                            VStack(alignment: .leading, spacing: 2) {
                                Text("\(code) \(item["name"].text)").font(.callout.monospacedDigit())
                                if let board = item["board_label"].string, !board.isEmpty { Text(board).font(.caption).foregroundStyle(.secondary) }
                            }
                        }
                        .contentShape(Rectangle())
                        .onTapGesture {
                            if selectedCodes.contains(code) { selectedCodes.remove(code) } else { selectedCodes.insert(code) }
                        }
                    }
                    ForEach(store.preview["errors"].array, id: \.anyHashKey) { error in
                        Text("第 \(error["line"].text) 行：\(error["reason"].text)\(error["code"].string.map { "（\($0)）" } ?? "")")
                            .font(.caption).foregroundStyle(.orange)
                    }
                }

                Section("评估日与模型") {
                    TextField("评估日 YYYY-MM-DD", text: $store.selectedEvalDate)
                        .keyboardType(.numbersAndPunctuation)
                    NavigationLink { CouncilModelOrderView(store: councilStore) } label: {
                        Label("模型与默认顺序（\(councilStore.selectedOrder.count)）", systemImage: "list.number")
                    }
                    if councilStore.selectedOrder.count < CouncilCore.requiredProfileCount {
                        Text("至少选择两个已配置有密钥的模型。").font(.caption).foregroundStyle(.orange)
                    }
                }

                Section {
                    Button {
                        if startValid { confirmStart = true }
                    } label: {
                        Label(store.starting ? "创建中…" : "确认并开始批次", systemImage: "play.fill")
                    }
                    .buttonStyle(.glassProminent).tint(Theme.gold)
                    .disabled(!startValid || store.starting)
                }
            }

            Section("批次列表") {
                if store.batches.isEmpty && store.lastError == nil && store.serverSupportsBatch {
                    Text("还没有批量任务。").font(.caption).foregroundStyle(.secondary)
                }
                ForEach(store.batches, id: \.councilID) { batch in
                    NavigationLink { CouncilBatchDetailView(batchID: batch.councilID, store: store, councilStore: councilStore, sessionLink: $sessionLink) } label: {
                        CouncilBatchRow(batch: batch)
                    }
                }
            }
        }
        .scrollContentBackground(.hidden)
        .screenBackground()
        .navigationTitle("批量讨论")
        .fileImporter(isPresented: $importerOpen, allowedContentTypes: [.plainText, .commaSeparatedText], allowsMultipleSelection: false) { result in
            Task { await importFile(result) }
        }
        .task(id: accountFingerprint) {
            store.rebind(server: state.selectedServer?.id ?? "", account: accountFingerprint)
            councilStore.rebind(server: state.selectedServer?.id ?? "", account: accountFingerprint)
            await councilStore.loadModels()
            await store.loadBatches(serverVersion: serverVersion)
        }
        .onChange(of: state.serverText) { _, _ in
            store.rebind(server: state.selectedServer?.id ?? "", account: accountFingerprint)
            councilStore.rebind(server: state.selectedServer?.id ?? "", account: accountFingerprint)
            Task {
                await councilStore.loadModels()
                await store.loadBatches(serverVersion: state.serverVersion)
            }
        }
        .refreshable {
            await councilStore.loadModels()
            await store.loadBatches(serverVersion: serverVersion, forceVersionCheck: false)
        }
        .confirmationDialog("开始批量讨论", isPresented: $confirmStart, titleVisibility: .visible) {
            Button("确认开始 \(selectedCodes.count) 只股票", role: .destructive) {
                Task { await store.start(profileIDs: councilStore.selectedOrder, evalDate: store.selectedEvalDate, selectedCodes: selectedCodes) }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("评估日 \(store.selectedEvalDate)，模型顺序 \(councilStore.selectedOrder.joined(separator: " → "))。开始后不可退回未启动项之外的并发费用；取消只影响 pending 项。")
        }
        .sheet(item: $sessionLink) { link in
            CouncilSessionSheet(sessionID: link.id, store: councilStore)
        }
    }

    private var startValid: Bool {
        !selectedCodes.isEmpty && !store.selectedEvalDate.isEmpty &&
        councilStore.selectedOrder.count >= CouncilCore.requiredProfileCount && !store.starting
    }

    private func importFile(_ result: Result<URL, Error>) async {
        do {
            let url = try result.get()
            let accessing = url.startAccessingSecurityScopedResource()
            do {
                let data = try Data(contentsOf: url, options: .mappedIfSafe)
                let ok = await store.fetchPreview(data: data, fileName: url.lastPathComponent)
                if ok {
                    selectedCodes = Set(store.preview["items"].array.map { $0["code"].text })
                } else if store.lastError == nil {
                    store.lastError = "没有有效候选，请检查表头「代码/名称」列。"
                }
            } catch {
                store.lastError = "读取文件失败：\(error.localizedDescription)"
            }
            if accessing { url.stopAccessingSecurityScopedResource() }
        } catch {
            store.lastError = error.localizedDescription
        }
    }
}

private struct CouncilBatchRow: View {
    let batch: JSON
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text(batch["source_name"].text).font(.headline)
                Spacer()
                Text(CouncilBatchCore.batchStatusLabel(batch["status"].string)).font(.caption.bold())
                    .foregroundStyle(statusColor)
            }
            Text("#\(batch["id"].text) · \(batch["source_date"].text) · \(batch["eval_date"].text)")
                .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            Text("完成 \(batch["done_count"].text) / 失败 \(batch["failed_count"].text) / 取消 \(batch["cancelled_count"].text) / 共 \(batch["item_count"].text)")
                .font(.caption).foregroundStyle(.secondary)
            if let error = batch["error"].string, !error.isEmpty { Text(error).font(.caption).foregroundStyle(.red).lineLimit(2) }
        }
    }
    private var statusColor: Color {
        switch batch["status"].string {
        case "running": return .green
        case "failed", "interrupted": return .orange
        case "done": return .blue
        default: return .secondary
        }
    }
}

private struct CouncilBatchDetailView: View {
    let batchID: Int
    let store: CouncilBatchStore
    let councilStore: CouncilStore
    @Binding var sessionLink: BatchSessionLink?
    @State private var confirmCancel = false
    @State private var confirmRetry = false
    @Environment(\.dismiss) private var dismiss

    private var batch: JSON { store.detailByID[batchID] ?? store.batches.first { $0.councilID == batchID } ?? .null }
    private var pendingCount: Int { batch["items"].array.filter { $0["status"].string == "pending" }.count }
    private var failedCount: Int { batch["items"].array.filter { $0["status"].string == "failed" }.count }
    private var runningCount: Int { batch["items"].array.filter { $0["status"].string == "running" }.count }

    var body: some View {
        List {
            Section {
                VStack(alignment: .leading, spacing: 6) {
                    Text("\(batch["source_name"].text) · #\(batch["id"].text)").font(.headline)
                    Text("数据日期 \(batch["source_date"].text) · 评估日 \(batch["eval_date"].text)").font(.caption)
                    Text("状态 \(CouncilBatchCore.batchStatusLabel(batch["status"].string)) · \(batch["stage"].text)").font(.callout)
                    if let error = batch["error"].string, !error.isEmpty { Text(error).font(.caption).foregroundStyle(.red) }
                    if batch["status"].string == "interrupted" {
                        Text("进程重启后批次已中断；系统不会自动重放付费模型调用。可对失败项显式重试，或查看已生成会话。")
                            .font(.caption).foregroundStyle(.orange)
                    }
                }
            }.listRowBackground(Color.clear)

            Section {
                if pendingCount > 0 {
                    Button("取消未开始项（\(pendingCount)）", role: .destructive) { confirmCancel = true }
                }
                if failedCount > 0 {
                    Button("重试失败项（\(failedCount)）") { confirmRetry = true }
                }
                if runningCount > 0 {
                    Button("刷新状态") { Task { await store.refreshDetail(id: batchID) } }
                }
            }

            Section("进度") {
                Text("完成 \(batch["done_count"].text) / 失败 \(batch["failed_count"].text) / 取消 \(batch["cancelled_count"].text) / 共 \(batch["item_count"].text)")
                    .font(.caption)
                ForEach(batch["items"].array, id: \.councilID) { item in
                    CouncilBatchItemRow(item: item, sessionLink: $sessionLink)
                }
            }
        }
        .scrollContentBackground(.hidden)
        .screenBackground()
        .navigationTitle("批次 #\(batch["id"].text)")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: batchID) {
            await store.refreshDetail(id: batchID)
            store.startPollIfNeeded()
        }
        .onDisappear { if store.runningBatchIDs.isEmpty { store.cancelPoll() } }
        .refreshable { await store.refreshDetail(id: batchID) }
        .confirmationDialog("取消未开始项", isPresented: $confirmCancel, titleVisibility: .visible) {
            Button("取消 \(pendingCount) 个 pending 项", role: .destructive) { Task { await store.cancel(id: batchID) } }
            Button("返回", role: .cancel) {}
        } message: {
            Text("不会停止已开始的单股讨论，也不删除任何历史会话。")
        }
        .confirmationDialog("重试失败项", isPresented: $confirmRetry, titleVisibility: .visible) {
            Button("重试 \(failedCount) 个 failed 项") { Task { await store.retryFailed(id: batchID) } }
            Button("返回", role: .cancel) {}
        } message: {
            Text("仅重置 failed 项；done 项不会重跑，不会隐式删除历史。")
        }
        .safeAreaInset(edge: .top, spacing: 0) {
            if let error = store.detailError[batchID], !error.isEmpty { ErrorBanner(message: error).padding(.horizontal) }
        }
    }
}

private struct CouncilBatchItemRow: View {
    let item: JSON
    @Binding var sessionLink: BatchSessionLink?
    @State private var showExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text("\(item["position"].text). \(item["stock_code"].text) \(item["stock_name"].text)")
                    .font(.callout.monospacedDigit())
                Spacer()
                Text(CouncilBatchCore.itemStatusLabel(item["status"].string)).font(.caption.bold()).foregroundStyle(statusColor)
            }
            if !item["stage"].text.isEmpty && item["stage"].text != "—" { Text(item["stage"].text).font(.caption).foregroundStyle(.secondary) }
            if let model = item["details"]["current_model"].string, !model.isEmpty { Text("当前模型 \(model)").font(.caption2).foregroundStyle(.secondary) }
            if !item["summary"].text.isEmpty && item["summary"].text != "—" {
                Text(item["summary"].text).font(.caption).lineLimit(3)
            }
            if !item["error"].text.isEmpty && item["error"].text != "—" { Text(item["error"].text).font(.caption).foregroundStyle(.red).lineLimit(2) }
            if item["status"].string == "done" || item["probability_percent"].double != nil {
                Text("概率 \(CouncilBatchCore.batchProbabilityText(item))").font(.caption).foregroundStyle(Theme.gold)
            }
            if let sessionID = item["session_id"].int {
                Button { sessionLink = BatchSessionLink(id: sessionID) } label: {
                    Label("打开单股讨论 #\(sessionID)", systemImage: "bubble.left.and.bubble.right")
                }.font(.caption)
                if item["details"]["session_deleted"].bool {
                    Text("原单股历史已删除或不可打开。").font(.caption2).foregroundStyle(.orange)
                }
            }
        }
        .padding(.vertical, 4)
    }

    private var statusColor: Color {
        switch item["status"].string {
        case "done": return .blue
        case "failed": return .red
        case "running": return .green
        case "cancelled": return .secondary
        default: return .orange
        }
    }
}

private extension JSON {
    var batchTextID: String { self["id"].text }
    var anyHashKey: String {
        [self["line"].text, self["code"].text, self["reason"].text].joined(separator: "|")
    }
}
