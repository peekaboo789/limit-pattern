import SwiftUI

/// AI 模型列表与一键切换（GET /api/ai/profiles，POST /api/ai/profiles/{id}/activate）。不读取也不发送密钥。
@MainActor
@Observable
final class ModelStore {
    static let shared = ModelStore()
    var items: [JSON] = []
    var activeName = ""
    var error: String?
    var toast: String?
    var busyId: String?

    static func provider(_ url: String) -> String {
        let h = URL(string: url)?.host?.lowercased() ?? url.lowercased()
        let map: [(String, String)] = [("aliyuncs", "阿里云百炼"), ("dashscope", "阿里云百炼"), ("deepseek", "DeepSeek"), ("openai", "OpenAI"),
                                       ("moonshot", "Moonshot"), ("bigmodel", "智谱"), ("volces", "火山方舟"), ("siliconflow", "硅基流动"),
                                       ("anthropic", "Anthropic"), ("googleapis", "Google"), ("x.ai", "xAI"), ("openrouter", "OpenRouter")]
        for (k, v) in map where h.contains(k) { return v }
        return h.isEmpty ? "—" : h
    }

    func load() async {
        do {
            let r = try await APIClient.shared.get("/api/ai/profiles")
            items = r["items"].array
            activeName = r["active_name"].string ?? items.first { $0["active"].bool }?["name"].string ?? ""
            error = nil
        } catch let e as APIError where e.status == 404 { error = "服务器还不支持多模型，请升级到 0.5.2 以上" }
        catch { self.error = error.localizedDescription }
    }

    func activate(_ p: JSON) async {
        let id = p["id"].text
        busyId = id; defer { busyId = nil }
        do {
            _ = try await APIClient.shared.post("/api/ai/profiles/\(id)/activate")
            await load()
            toast = "已切换到 \(p["name"].text)"
            Notifier.hapticAlert()
            Task { try? await Task.sleep(for: .seconds(2.5)); toast = nil }
        } catch { self.error = error.localizedDescription }
    }
}

struct ModelListView: View {
    @State private var store = ModelStore.shared
    @State private var confirm: JSON?

    var body: some View {
        List {
            Section {
                ErrorBanner(message: store.error).listRowBackground(Color.clear)
                if store.items.isEmpty && store.error == nil { Text("还没有添加模型。在「设置」网页里添加后这里就会出现。").font(.caption).foregroundStyle(.secondary) }
            }
            Section("点一行切换当前模型") {
                ForEach(store.items, id: \.self["id"].text) { p in
                    Button { if !p["active"].bool { confirm = p } } label: {
                        HStack(spacing: 12) {
                            Image(systemName: p["active"].bool ? "checkmark.circle.fill" : "circle")
                                .foregroundStyle(p["active"].bool ? Theme.gold : .secondary).font(.title3)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(p["name"].text).font(.headline)
                                Text(p["model"].text).font(.caption.monospaced()).foregroundStyle(.secondary)
                                HStack(spacing: 8) {
                                    Text(ModelStore.provider(p["base_url"].string ?? "")).font(.caption2)
                                    if !p["api_key_set"].bool { Text("未填密钥").font(.caption2).foregroundStyle(.orange) }
                                }.foregroundStyle(.secondary)
                            }
                            Spacer()
                            if store.busyId == p["id"].text { ProgressView() }
                            else if p["active"].bool { Text("✅ 当前").font(.caption.bold()).foregroundStyle(Theme.gold) }
                        }
                    }
                    .foregroundStyle(.primary)
                    .listRowBackground(Color.clear)
                }
            }
        }
        .scrollContentBackground(.hidden)
        .screenBackground()
        .navigationTitle("AI 模型")
        .task { await store.load() }
        .refreshable { await store.load() }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                NavigationLink { WebPageView(title: "设置", path: "/settings") } label: { Label("管理", systemImage: "gearshape") }
            }
        }
        .confirmationDialog("切换 AI 模型", isPresented: Binding(get: { confirm != nil }, set: { if !$0 { confirm = nil } }), titleVisibility: .visible) {
            if let p = confirm { Button("切换到 \(p["name"].text)") { Task { await store.activate(p) } } }
            Button("取消", role: .cancel) {}
        } message: { Text("之后生成的 AI 榜单和每日总结会用这个模型。") }
        .modelToast()
    }
}

/// AI 榜单页右上角：显示当前模型，点开直接切换。
struct ModelMenu: View {
    @State private var store = ModelStore.shared
    var body: some View {
        Menu {
            Section("当前：\(store.activeName.isEmpty ? "未设置" : store.activeName)") {
                ForEach(store.items, id: \.self["id"].text) { p in
                    Button { Task { await store.activate(p) } } label: {
                        if p["active"].bool { Label(p["name"].text, systemImage: "checkmark") } else { Text(p["name"].text) }
                    }
                    .disabled(p["active"].bool)
                }
            }
            if let e = store.error { Text(e) }
        } label: {
            Label(store.activeName.isEmpty ? "模型" : store.activeName, systemImage: "cpu")
                .labelStyle(.titleAndIcon).lineLimit(1).frame(maxWidth: 120)
        }
        .task { await store.load() }
    }
}

struct ModelToast: ViewModifier {
    @State private var store = ModelStore.shared
    func body(content: Content) -> some View {
        content.overlay(alignment: .top) {
            if let t = store.toast {
                Label(t, systemImage: "checkmark.circle.fill").font(.callout.bold())
                    .padding(.horizontal, 16).padding(.vertical, 10)
                    .glassEffect(.regular.tint(Theme.gold.opacity(0.3)), in: .capsule)
                    .padding(.top, 8).transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .animation(.spring, value: store.toast)
    }
}
extension View { func modelToast() -> some View { modifier(ModelToast()) } }
