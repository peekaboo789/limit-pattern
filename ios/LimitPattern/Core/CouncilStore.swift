import Foundation
import Observation

/// 多模型讨论的原生状态与网络层。所有请求只发往 APIClient 当前 baseURL，
/// 与当前登录身份绑定；切服务器/账号/登出后由 identity 校验和 generation 丢弃迟到响应。
@MainActor
@Observable
final class CouncilStore {
    static let shared = CouncilStore()
    var api = APIClient.shared

    var models: [JSON] = []
    var order: [String] = []
    var sessions: [JSON] = []
    var progress: JSON = .null
    var sources: JSON = .null
    var lastError: String?
    var hint: String?
    var detailError: [Int: String] = [:]
    var detailHint: [Int: String] = [:]
    var deleting: Set<Int> = []
    var busyID: Int?
    var sourceBusy = false

    private var historyGeneration = 0
    private var pollTask: Task<Void, Never>?
    private var pollGeneration = 0
    private var pollActive = false
    private var pollFailures = 0
    private(set) var identityFingerprint = ""
    private var identityGeneration = 0
    private(set) var serverSupportsCouncil = false

    var keyModels: [JSON] { models.filter { $0["api_key_set"].bool } }
    var selectedOrder: [String] { order.filter { id in keyModels.contains { $0["id"].string == id } } }
    var canStart: Bool { !selectedOrder.isEmpty && selectedOrder.count >= CouncilCore.requiredProfileCount && !(progress["running"].bool) && busyID == nil }
    var activeSessionID: Int? { progress["session_id"].int }

    struct Identity: Equatable {
        let server: String
        let account: String
        var fingerprint: String { "\(server)|\(account)" }
    }

    func rebind(server: String, account: String) {
        let identity = Identity(server: server, account: account)
        let newFingerprint = identity.fingerprint
        guard newFingerprint != identityFingerprint else { return }
        identityFingerprint = newFingerprint
        identityGeneration += 1
        cancelPoll()
        models = []
        order = []
        sessions = []
        progress = .null
        sources = .null
        detailError = [:]
        detailHint = [:]
        busyID = nil
        hint = nil
        serverSupportsCouncil = false
        historyGeneration += 1
        pollFailures = 0
    }

    func supportsCouncil(serverVersion: String) -> Bool {
        !serverVersion.isEmpty && APIClient.compareVersion(serverVersion, "0.7.18") >= 0
    }

    func loadAll(serverVersion: String) async {
        serverSupportsCouncil = supportsCouncil(serverVersion: serverVersion)
        if !serverSupportsCouncil {
            lastError = CouncilCore.serverUpgradeMessage
            return
        }
        lastError = nil
        await loadModels()
        await loadSessions()
    }

    func loadModels() async {
        let generation = identityGeneration
        do {
            let items = try await api.get("/api/council/models")["items"].array
            guard generation == identityGeneration else { return }
            models = items; lastError = nil
        } catch { lastError = error.localizedDescription }
        if !order.isEmpty { return }
        do {
            let ids = try await api.get("/api/council/profile-order")["profile_ids"].array.compactMap(\.string)
            guard generation == identityGeneration else { return }
            order = ids
        } catch { }
    }

    func loadSessions() async {
        let generation = identityGeneration
        do {
            let response = try await api.get("/api/council/history", ["limit": "100"], timeout: 20)
            guard generation == identityGeneration else { return }
            sessions = response["items"].array
            lastError = nil
        } catch let api as APIError where api.status == 404 {
            serverSupportsCouncil = false
            lastError = CouncilCore.serverUpgradeMessage
        } catch { lastError = error.localizedDescription }
    }

    func loadSources() async {
        let generation = identityGeneration
        sourceBusy = true; defer { sourceBusy = false }
        do {
            let result = try await api.get("/api/council/sources", timeout: 15)
            guard generation == identityGeneration else { return }
            sources = result; lastError = nil
        }
        catch let api as APIError where api.status == 404 { lastError = CouncilCore.serverUpgradeMessage }
        catch { lastError = error.localizedDescription }
    }

    func sourceAction(_ path: String, method: String = "GET", body: [String: Any]? = nil) async {
        let generation = identityGeneration
        sourceBusy = true; defer { sourceBusy = false }
        do {
            let result: JSON
            switch method {
            case "POST": result = try await api.post("/api/council/sources\(path)", body ?? [:], timeout: 15)
            case "PUT": result = try await api.put("/api/council/sources\(path)", body ?? [:], timeout: 15)
            default: result = try await api.get("/api/council/sources\(path)", timeout: 15)
            }
            guard generation == identityGeneration else { return }
            sources = result
            lastError = nil
            if path == "/check-update" {
                if let candidate = sources["candidate_version"].string { hint = "已验证清单 \(candidate)，点击应用生效。" }
                else { hint = "没有更高版本，保留当前配置。" }
            } else if method != "GET" {
                hint = "已原子保存，仅影响下一次讨论采集，不改已有会话证据。"
            }
        } catch { lastError = "\(error.localizedDescription)；当前清单保留，断网可继续使用内置/上次版本。" }
    }

    func saveOrder() async {
        let generation = identityGeneration
        do {
            let r = try await api.put("/api/council/profile-order", ["profile_ids": order])
            guard generation == identityGeneration else { return }
            order = r["profile_ids"].array.compactMap(\.string)
            hint = "已保存默认开会顺序"
            lastError = nil
        } catch { hint = error.localizedDescription }
    }

    func run(code: String, date: String?) async -> Bool {
        let generation = identityGeneration
        lastError = nil
        var body: [String: Any] = ["code": code.trimmingCharacters(in: .whitespacesAndNewlines), "profile_ids": selectedOrder]
        if let date, !date.isEmpty { body["date"] = date }
        busyID = -1
        defer { busyID = nil }
        do {
            let r = try await api.post("/api/council/run", body, timeout: 30)
            guard generation == identityGeneration, !identityFingerprint.isEmpty else { return false }
            let sessionID = r["session_id"].int
            hint = sessionID.map { "已开始讨论 #\($0)，正在联网核对" } ?? "已开始讨论"
            if let sessionID { await loadSession(id: sessionID) }
            await loadSessions()
            beginPollingIfNeeded()
            return sessionID != nil
        } catch { lastError = error.localizedDescription; return false }
    }

    func loadSession(id: Int) async {
        await refreshDetail(id: id)
    }

    func refreshDetail(id: Int) async {
        guard !identityFingerprint.isEmpty else { return }
        let generation = identityGeneration
        do {
            let r = try await api.get("/api/council/\(id)", timeout: 20)
            guard generation == identityGeneration else { return }
            detailError[id] = nil
            if let index = sessions.firstIndex(where: { $0["id"].int == id }) { sessions[index] = r } else { sessions.insert(r, at: 0) }
        } catch { detailError[id] = error.localizedDescription }
    }

    func deleteSession(id: Int) async -> Bool {
        guard !identityFingerprint.isEmpty else { return false }
        let generation = identityGeneration
        guard !deleting.contains(id), !(progress["running"].bool && progress["session_id"].int == id) else { return false }
        deleting.insert(id); defer { deleting.remove(id) }
        do {
            _ = try await api.delete("/api/council/\(id)")
            guard generation == identityGeneration else { return false }
            sessions.removeAll { $0["id"].int == id }
            detailError[id] = nil
            return true
        } catch { detailError[id] = error.localizedDescription; return false }
    }

    func addUserMessage(id: Int, text: String) async {
        guard !identityFingerprint.isEmpty else { return }
        let generation = identityGeneration
        let submitted = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !submitted.isEmpty else { detailError[id] = "意见不能为空"; return }
        guard submitted.count <= CouncilCore.maxUserMessageLength else { detailError[id] = "意见最多 \(CouncilCore.maxUserMessageLength) 字"; return }
        detailError[id] = nil; detailHint[id] = nil
        do {
            let r = try await api.post("/api/council/\(id)/user-message", ["text": submitted], timeout: 30)
            guard generation == identityGeneration else { return }
            guard r["id"].int == id else { throw APIError(status: 0, message: "响应会话不匹配") }
            replaceSession(r)
            detailHint[id] = "用户意见已写入对话"
        } catch {
            detailError[id] = "\(error.localizedDescription)；草稿已保留。"
        }
    }

    func speak(id: Int, profileID: String, round: Int? = nil) async {
        guard !identityFingerprint.isEmpty else { return }
        let generation = identityGeneration
        detailError[id] = nil; detailHint[id] = nil
        var body: [String: Any] = ["profile_id": profileID]
        if let round { body["round"] = round }
        do {
            let r = try await api.post("/api/council/\(id)/speak", body, timeout: 135)
            guard generation == identityGeneration else { return }
            guard r["id"].int == id else { throw APIError(status: 0, message: "响应会话不匹配") }
            replaceSession(r)
            await refreshDetail(id: id)
            detailHint[id] = r["status"].string == "failed" ? "模型发言失败，详见对话记录" : "模型已完成发言"
        } catch { detailError[id] = error.localizedDescription }
    }

    func reconcileStale() async {
        let generation = identityGeneration
        do {
            let r = try await api.post("/api/council/reconcile-stale", timeout: 10)
            guard generation == identityGeneration else { return }
            let terminated = r["terminated"].int ?? 0
            await loadSessions()
            hint = terminated > 0 ? "已终止 \(terminated) 场遗留运行中会话" : "没有遗留运行中会话"
        } catch { lastError = error.localizedDescription }
    }

    private func replaceSession(_ r: JSON) {
        guard !identityFingerprint.isEmpty else { return }
        guard let id = r["id"].int else { return }
        if let index = sessions.firstIndex(where: { $0["id"].int == id }) { sessions[index] = r } else { sessions.insert(r, at: 0) }
    }

    func beginPollingIfNeeded() { if progress["running"].bool || busyID != nil { startPoll() } }
    func startPoll() {
        pollActive = true
        if pollTask != nil { return }
        let generation = pollGeneration + 1
        pollGeneration = generation
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                if self.pollGeneration != generation { return }
                if identityFingerprint.isEmpty { return }
                await self.pollTick(generation: generation)
                guard self.pollGeneration == generation else { return }
                try? await Task.sleep(for: .seconds(1.5))
            }
        }
    }
    func cancelPoll() { pollActive = false; pollGeneration += 1; pollTask?.cancel(); pollTask = nil }

    private func pollTick(generation: Int) async {
        guard api.baseURL != nil, pollGeneration == generation else { return }
        do {
            let next = try await api.get("/api/council/progress", timeout: 5)
            guard pollGeneration == generation else { return }
            progress = next
            if !(next["running"].bool) && busyID == nil { cancelPoll(); return }
            if let id = next["session_id"].int { await refreshDetail(id: id) }
            pollFailures = 0
        } catch {
            pollFailures += 1
            if pollFailures >= 3 { cancelPoll(); lastError = "自动刷新失败，请点击刷新/重试" }
        }
    }
}
