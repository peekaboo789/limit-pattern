import Foundation
import Observation

/// 批量讨论状态与网络层。沿用 CouncilStore 的服务器+账户身份指纹模式，
/// 所有请求由当前 `APIClient` 发出；切身份后迟到响应直接丢弃。
@MainActor
@Observable
final class CouncilBatchStore {
    static let shared = CouncilBatchStore()
    var api = APIClient.shared

    var preview: JSON = .null
    var batches: [JSON] = []
    var lastError: String?
    var hint: String?
    var previewLoading = false
    var starting = false
    var selectedEvalDate = ""
    var selectedProfileIDs: [String] = []
    var order: [String] = []
    var detailByID: [Int: JSON] = [:]
    var detailError: [Int: String] = [:]
    var busy: Set<Int> = []

    private var identityFingerprint = ""
    private var identityGeneration = 0
    private var pollTask: Task<Void, Never>?
    private var pollGeneration = 0
    private var pollActive = false
    private var pollFailures = 0
    private(set) var serverSupportsBatch = false

    var runningBatchIDs: [Int] { batches.compactMap { $0["status"].string == "running" ? $0["id"].int : nil } }

    struct Identity: Equatable {
        let server: String
        let account: String
        var fingerprint: String { "\(server)|\(account)" }
    }

    func rebind(server: String, account: String) {
        let fingerprint = Identity(server: server, account: account).fingerprint
        guard fingerprint != identityFingerprint else { return }
        identityFingerprint = fingerprint
        identityGeneration += 1
        cancelPoll()
        preview = .null
        batches = []
        detailByID = [:]
        detailError = [:]
        busy = []
        lastError = nil
        hint = nil
        selectedEvalDate = ""
        selectedProfileIDs = []
        order = []
        serverSupportsBatch = false
        pollFailures = 0
    }

    func loadBatches(serverVersion: String, forceVersionCheck: Bool = true) async {
        let generation = identityGeneration
        if forceVersionCheck {
            serverSupportsBatch = !serverVersion.isEmpty && APIClient.compareVersion(serverVersion, "0.7.18") >= 0
            guard serverSupportsBatch else {
                lastError = CouncilBatchCore.batchUpgradeMessage
                batches = []
                return
            }
        }
        do {
            let result = try await api.get("/api/council/batch", ["limit": "50"], timeout: 20)
            guard generation == identityGeneration else { return }
            batches = result["items"].array
            lastError = nil
        } catch let api as APIError where api.status == 404 {
            serverSupportsBatch = false
            lastError = CouncilBatchCore.batchUpgradeMessage
        } catch {
            lastError = error.localizedDescription
        }
    }

    func fetchPreview(data: Data, fileName: String) async -> Bool {
        let generation = identityGeneration
        previewLoading = true; defer { previewLoading = false }
        do {
            let result = try await api.jsonData("POST", "/api/council/batch/preview",
                                                body: data,
                                                headers: ["x-file-name": CouncilBatchCore.headerFileName(fileName)],
                                                timeout: 30)
            guard generation == identityGeneration else { return false }
            preview = result
            selectedEvalDate = result["default_eval_date"].string ?? result["source_date"].string ?? ""
            lastError = nil
            return !result["items"].array.isEmpty
        } catch let api as APIError where api.status == 404 {
            serverSupportsBatch = false
            lastError = CouncilBatchCore.batchUpgradeMessage
            return false
        } catch {
            lastError = error.localizedDescription
            return false
        }
    }

    func start(profileIDs: [String], evalDate: String, selectedCodes: Set<String>? = nil) async -> Bool {
        guard !identityFingerprint.isEmpty else { return false }
        let generation = identityGeneration
        starting = true; defer { starting = false }
        let selectedSet = selectedCodes ?? Set(CouncilBatchCore.selectedCodes(preview))
        let codes = preview["items"].array.compactMap { item -> String? in
            guard let code = item["code"].string, selectedSet.contains(code) else { return nil }
            return code
        }
        let bodyItems = preview["items"].array.map { item in
            var object = item.object
            object["selected"] = .bool(selectedCodes?.contains(item["code"].text) ?? true)
            return JSON.object(object)
        }
        let bodyPreview = preview.object.merging(["items": .array(bodyItems)]) { _, new in new }
        guard !codes.isEmpty else { lastError = "请至少选择一只股票"; return false }
        guard !evalDate.isEmpty else { lastError = "请填写评估日"; return false }
        let ordered = orderedProfileIDs(from: profileIDs)
        guard ordered.count >= CouncilCore.requiredProfileCount else { lastError = "至少选择两个已配置有密钥的模型"; return false }
        let key = CouncilBatchCore.idempotencyKey(preview: JSON.object(bodyPreview), evalDate: evalDate, profileIDs: ordered)
        let body: [String: Any] = [
            "preview": JSON.object(bodyPreview).any,
            "eval_date": evalDate,
            "profile_ids": ordered,
            "source_name": preview["source_name"].string ?? "",
            "idempotency_key": key
        ]
        do {
            let result = try await api.post("/api/council/batch/start", body, timeout: 30)
            guard generation == identityGeneration, !identityFingerprint.isEmpty else { return false }
            upsertBatch(result)
            hint = "已创建批次 #\(result["id"].text)，可双击防重复"
            lastError = nil
            startPollIfNeeded()
            return true
        } catch let api as APIError where api.status == 404 {
            serverSupportsBatch = false
            lastError = CouncilBatchCore.batchUpgradeMessage
            return false
        } catch {
            lastError = error.localizedDescription
            return false
        }
    }

    func refreshDetail(id: Int) async {
        guard !identityFingerprint.isEmpty else { return }
        let generation = identityGeneration
        do {
            let result = try await api.get("/api/council/batch/\(id)", timeout: 20)
            guard generation == identityGeneration else { return }
            detailByID[id] = result
            detailError[id] = nil
            replaceBatch(result)
        } catch {
            detailError[id] = error.localizedDescription
        }
    }

    func cancel(id: Int) async {
        guard !identityFingerprint.isEmpty, !busy.contains(id) else { return }
        let generation = identityGeneration
        busy.insert(id); defer { busy.remove(id) }
        do {
            let result = try await api.post("/api/council/batch/\(id)/cancel", timeout: 20)
            guard generation == identityGeneration else { return }
            replaceBatch(result)
            detailByID[id] = result
            hint = "已取消 \(result["cancelled_now"].text) 个待开始项"
        } catch {
            lastError = error.localizedDescription
        }
    }

    func retryFailed(id: Int) async {
        guard !identityFingerprint.isEmpty, !busy.contains(id) else { return }
        let generation = identityGeneration
        busy.insert(id); defer { busy.remove(id) }
        do {
            let result = try await api.post("/api/council/batch/\(id)/retry-failed", timeout: 20)
            guard generation == identityGeneration else { return }
            replaceBatch(result)
            detailByID[id] = result
            hint = "已重置 \(result["retried"].text) 个失败项"
            startPollIfNeeded()
        } catch {
            lastError = error.localizedDescription
        }
    }

    func beginPollingIfNeeded() { startPollIfNeeded() }

    func startPollIfNeeded() {
        if runningBatchIDs.isEmpty { return }
        if pollTask != nil { return }
        pollActive = true
        let generation = pollGeneration + 1
        pollGeneration = generation
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                if self.pollGeneration != generation { return }
                if self.identityFingerprint.isEmpty { return }
                await self.pollTick(generation: generation)
                guard self.pollGeneration == generation else { return }
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }

    func cancelPoll() {
        pollActive = false
        pollGeneration += 1
        pollTask?.cancel()
        pollTask = nil
    }

    private func pollTick(generation: Int) async {
        guard api.baseURL != nil, pollGeneration == generation else { return }
        do {
            let result = try await api.get("/api/council/batch", ["limit": "50"], timeout: 8)
            guard pollGeneration == generation else { return }
            batches = result["items"].array
            for id in runningBatchIDs {
                if let row = batches.first(where: { $0["id"].int == id }) {
                    detailByID[id] = row
                    replaceBatch(row)
                }
            }
            pollFailures = 0
            if runningBatchIDs.isEmpty { cancelPoll() }
        } catch {
            pollFailures += 1
            if pollFailures >= 3 { cancelPoll(); lastError = "自动刷新失败，请手动下拉刷新" }
        }
    }

    private func orderedProfileIDs(from selected: [String]) -> [String] {
        let filtered = selected.filter { !$0.isEmpty }
        if filtered.count >= 2 { return filtered }
        return []
    }

    private func replaceBatch(_ result: JSON) {
        guard let id = result["id"].int else { return }
        if let index = batches.firstIndex(where: { $0["id"].int == id }) { batches[index] = result } else { batches.insert(result, at: 0) }
    }

    private func upsertBatch(_ result: JSON) {
        guard let id = result["id"].int else { return }
        if let index = batches.firstIndex(where: { $0["id"].int == id }) { batches[index] = result } else { batches.insert(result, at: 0) }
        detailByID[id] = result
    }
}
