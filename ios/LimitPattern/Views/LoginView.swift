import SwiftUI

@MainActor
struct LoginView: View {
    @Environment(AppState.self) private var state
    @State private var password = ""
    @FocusState private var focus: Int?
    @State private var serverEditor: ServerEditor?
    @State private var reachable: ServerReachability?
    @State private var swipedLoginID: String?
    @State private var deleteCandidate: SavedLogin?

    var body: some View {
        @Bindable var state = state
        ScrollView {
            VStack(spacing: 20) {
                header
                serverSection
                loginSection(username: $state.username)
                notes
                savedLoginsSection
            }
            .padding(22).frame(maxWidth: 520)
            .frame(maxWidth: .infinity)
        }
        .scrollDismissesKeyboard(.interactively)
        .screenBackground()
        .sheet(item: $serverEditor) { editor in
            serverEditorSheet(editor)
        }
        .onChange(of: state.selectedServer?.id) { _, _ in reachable = nil }
    }

    private var header: some View {
        VStack(spacing: 8) {
            Image(systemName: "chart.bar.fill").font(.system(size: 54)).foregroundStyle(Theme.gold)
                .padding(26).glassEffect(.regular.tint(Theme.gold.opacity(0.2)), in: .circle)
                .padding(.top, 24)
            Text("涨停形态").font(.largeTitle.bold())
            Text(state.setupNeeded ? "首次使用：创建管理员账号" : "连接你的 Docker 版服务器")
                .foregroundStyle(state.setupNeeded ? Theme.gold : .secondary)
        }
    }

    private var serverSection: some View {
        GlassEffectContainer(spacing: 10) {
            VStack(spacing: 10) {
                serverPicker
                serverActions
            }
        }
    }

    private var serverPicker: some View {
        Menu {
            if state.servers.servers.isEmpty {
                Text("还没有服务器")
            } else {
                ForEach(state.servers.servers) { server in
                    Button {
                        if server.id != state.servers.selectedID {
                            state.selectServer(server)
                        }
                    } label: {
                        if server.id == state.servers.selectedID { Label(server.name, systemImage: "checkmark") }
                        else { Text(server.name) }
                    }
                }
            }
            Divider()
            Button { serverEditor = ServerEditor(name: "", baseURL: "") } label: {
                Label("添加服务器", systemImage: "plus")
            }
        } label: {
            fieldRow(icon: "server.rack", title: "服务器") {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(state.selectedServer?.name ?? "选择服务器").font(.body.weight(.medium))
                        Text(state.serverText).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer()
                    Image(systemName: "chevron.up.chevron.down").foregroundStyle(.secondary)
                }
            }
        }
    }

    @ViewBuilder
    private var serverActions: some View {
        if let server = state.selectedServer {
            HStack(spacing: 10) {
                Button { checkReachability(server) } label: { Label(reachLabel(server), systemImage: reachIcon(server)).font(.caption) }
                    .buttonStyle(.glass)
                Spacer()
                Button { serverEditor = ServerEditor(serverID: server.id, name: server.name, baseURL: server.baseURL) } label: {
                    Label("编辑", systemImage: "pencil").font(.caption)
                }.buttonStyle(.glass)
                Button(role: .destructive) { state.removeServer(server.id) } label: { Label("删除", systemImage: "trash").font(.caption) }.buttonStyle(.glass)
            }
        }
    }

    private var savedLoginsSection: some View {
        Group {
            if state.hasSavedLogins {
                GlassEffectContainer(spacing: 8) {
                    VStack(spacing: 8) {
                        HStack {
                            Text("已保存的登录").font(.headline)
                            Spacer()
                            Text("轻点即可登录").font(.caption).foregroundStyle(.secondary)
                        }
                        ForEach(state.savedLogins) { login in
                            SavedLoginCard(
                                login: login,
                                isLoggingIn: state.loggingInKey == login.id,
                                swipedLoginID: $swipedLoginID,
                                onLogin: { Task { await quickLogin(login.account) } },
                                onDelete: { deleteCandidate = login }
                            )
                        }
                    }
                }
            }
        }
        .confirmationDialog("删除已保存的登录？", isPresented: Binding(
            get: { deleteCandidate != nil },
            set: { if !$0 { deleteCandidate = nil } }
        ), titleVisibility: .visible) {
            Button("删除", role: .destructive) {
                if let login = deleteCandidate {
                    state.accounts.remove(serverID: login.account.serverID, username: login.account.username)
                    if swipedLoginID == login.id { swipedLoginID = nil }
                }
                deleteCandidate = nil
            }
            Button("取消", role: .cancel) { deleteCandidate = nil }
        } message: {
            if let login = deleteCandidate {
                Text("将同时删除「\(login.server.name) · \(login.username)」的密码和登录状态。")
            }
        }
    }

    private func loginSection(username: Binding<String>) -> some View {
        GlassEffectContainer(spacing: 12) {
            VStack(spacing: 12) {
                field("账号", systemImage: "person") {
                    TextField(state.needsUsername ? "管理员账号" : "账号（旧版服务器可留空）", text: username)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().textContentType(.username).focused($focus, equals: 1)
                }
                field("密码", systemImage: "lock") {
                    SecureField("密码", text: $password).textContentType(.password).focused($focus, equals: 2)
                        .onSubmit { Task { await state.connect(password: password) } }
                }
                ErrorBanner(message: state.loginError)
                Button {
                    focus = nil
                    Task {
                        await state.connect(password: password)
                        if state.phase == .ready { password = "" }
                    }
                } label: {
                    HStack { if state.busy { ProgressView() }; Text(state.busy ? "连接中…" : (state.setupNeeded ? "创建账号并登录" : "登录")).bold() }.frame(maxWidth: .infinity).padding(.vertical, 6)
                }
                .buttonStyle(.glassProminent).tint(Theme.gold).disabled(state.busy)
            }
        }
    }

    private func serverEditorSheet(_ editor: ServerEditor) -> some View {
        let activate = editor.serverID == nil || editor.serverID == state.selectedServer?.id
        let previousURL = state.selectedServer?.baseURL
        return ServerEditorSheet(editor: editor, store: state.servers) { server in
            state.reconcileServer(server, activate: activate, previousURL: previousURL)
        }
    }

    private var notes: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("· 登录成功后会按服务器保存账户，下次轻点即可登录。")
            Text("· 服务器切换后各自独立会话，不会串登录状态。")
            Text("· 局域网可以用 http://IP:端口；外网建议用 HTTPS 反向代理。")
            Text("· 服务器没设密码时，账号密码留空直接点登录。")
        }.font(.footnote).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
    }

    private func field<C: View>(_ title: String, systemImage: String, @ViewBuilder _ content: () -> C) -> some View {
        fieldRow(icon: systemImage, title: title, content: content)
    }

    private func fieldRow<C: View>(icon: String, title: String, @ViewBuilder content: () -> C) -> some View {
        HStack(spacing: 12) {
            Image(systemName: icon).frame(width: 22).foregroundStyle(Theme.gold)
            content()
        }
        .padding(.horizontal, 16).padding(.vertical, 14)
        .glassEffect(.regular.interactive(), in: .rect(cornerRadius: 18))
        .accessibilityLabel(title)
    }

    private func quickLogin(_ account: SavedAccount) async {
        let outcome = await state.quickLogin(account)
        if outcome != .ready {
            focus = 2
        }
    }

    private func checkReachability(_ server: Server) {
        reachable = .checking
        Task {
            let temp = APIClient(storage: HTTPCookieStorage.shared, keychain: KeychainStore.shared)
            guard let url = APIClient.normalize(server.baseURL) else {
                reachable = .bad
                return
            }
            temp.baseURL = url
            do {
                _ = try await temp.get("/api/health", timeout: 8)
                reachable = .ok
            } catch {
                reachable = .bad
            }
        }
    }

    private func reachIcon(_ server: Server) -> String {
        if reachable == nil { return "antenna.radiowaves.left.and.right" }
        if reachable == .checking { return "hourglass" }
        return reachable == .ok ? "checkmark.circle" : "xmark.circle"
    }
    private func reachLabel(_ server: Server) -> String {
        if reachable == nil { return "检查可达性" }
        if reachable == .checking { return "检查中…" }
        return reachable == .ok ? "可达" : "不可达"
    }
}

@MainActor
private struct SavedLoginCard: View {
    let login: SavedLogin
    let isLoggingIn: Bool
    @Binding var swipedLoginID: String?
    let onLogin: () -> Void
    let onDelete: () -> Void

    @State private var offsetX: CGFloat = 0
    private let actionWidth: CGFloat = 76

    var body: some View {
        ZStack(alignment: .trailing) {
            Button(role: .destructive) {
                onDelete()
            } label: {
                VStack(spacing: 3) {
                    Image(systemName: "trash")
                    Text("删除").font(.caption)
                }
                .foregroundStyle(.white)
                .frame(width: actionWidth)
                .frame(maxHeight: .infinity)
                .background(.red.opacity(0.85), in: RoundedRectangle(cornerRadius: 16))
            }

            HStack(spacing: 12) {
                Image(systemName: "person.crop.circle.fill")
                    .font(.title3).foregroundStyle(Theme.gold)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(login.server.name).font(.body.weight(.semibold)).lineLimit(1)
                        Text(login.server.baseURL).font(.caption.monospacedDigit()).foregroundStyle(.secondary).lineLimit(1)
                    }
                    HStack(spacing: 6) {
                        Text(login.username).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                        Text(login.lastUsed, format: Date.RelativeFormatStyle(presentation: .named)).font(.caption2).foregroundStyle(.tertiary)
                    }
                }
                Spacer()
                if isLoggingIn {
                    ProgressView()
                } else {
                    Image(systemName: "arrow.right.circle").foregroundStyle(Theme.gold)
                }
            }
            .padding(.horizontal, 14).padding(.vertical, 12)
            .glassEffect(.regular.interactive(), in: .rect(cornerRadius: 16))
            .contentShape(Rectangle())
            .offset(x: offsetX)
            .gesture(
                DragGesture(minimumDistance: 20)
                    .onChanged { value in
                        let proposed = min(0, max(-actionWidth, value.translation.width))
                        offsetX = proposed
                    }
                    .onEnded { value in
                        let open = value.translation.width < -35
                        withAnimation(.spring(duration: 0.22)) {
                            offsetX = open ? -actionWidth : 0
                        }
                        swipedLoginID = open ? login.id : nil
                    }
            )
            .onTapGesture {
                if offsetX < 0 {
                    withAnimation(.spring(duration: 0.22)) { offsetX = 0 }
                    swipedLoginID = nil
                } else {
                    onLogin()
                }
            }
            .contextMenu {
                Button { onLogin() } label: { Label("登录", systemImage: "arrow.right.circle") }
                Button(role: .destructive) { onDelete() } label: { Label("删除", systemImage: "trash") }
            }
            .onChange(of: swipedLoginID) { _, newValue in
                if newValue != login.id, offsetX < 0 {
                    withAnimation(.spring(duration: 0.22)) { offsetX = 0 }
                }
            }
        }
        .clipped()
    }
}


enum ServerReachability: Equatable { case checking, ok, bad }

struct ServerEditor: Identifiable {
    let id = UUID()
    var serverID: String?
    var name: String
    var baseURL: String

    init(serverID: String? = nil, name: String, baseURL: String) {
        self.serverID = serverID
        self.name = name
        self.baseURL = baseURL
    }
}

@MainActor
struct ServerEditorSheet: View {
    let editor: ServerEditor
    let store: ServerStore
    let onSaved: (Server) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var name: String
    @State private var baseURL: String
    @State private var error: String?

    init(editor: ServerEditor, store: ServerStore, onSaved: @escaping (Server) -> Void = { _ in }) {
        self.editor = editor
        self.store = store
        self.onSaved = onSaved
        _name = State(initialValue: editor.name)
        _baseURL = State(initialValue: editor.baseURL)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("名称", text: $name)
                    TextField("https://gp.peekaboo789.site:2096", text: $baseURL)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                }
                if let error { Text(error).font(.caption).foregroundStyle(.red) }
            }
            .scrollContentBackground(.hidden)
            .screenBackground()
            .navigationTitle(editor.name.isEmpty ? "添加服务器" : "编辑服务器")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") {
                        if let message = ServerURL.validate(baseURL) {
                            error = message
                            return
                        }
                        if let server = try? store.upsert(id: editor.serverID, name: name, baseURL: baseURL) {
                            onSaved(server)
                        }
                        dismiss()
                    }
                }
            }
        }
    }
}
