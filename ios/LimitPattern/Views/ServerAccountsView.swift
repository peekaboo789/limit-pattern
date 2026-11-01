import SwiftUI

@MainActor
struct ServerAccountsView: View {
    @Environment(AppState.self) private var state
    @State private var editor: ServerEditor?

    var body: some View {
        List {
            Section("当前服务器") {
                if let server = state.selectedServer {
                    LabeledContent("名称", value: server.name)
                    LabeledContent("地址", value: server.baseURL)
                    Button("重新连接") { Task { await state.bootstrap() } }
                } else {
                    Text("还没有服务器").foregroundStyle(.secondary)
                }
            }

            Section("服务器列表") {
                ForEach(state.servers.servers) { server in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(server.name).font(.body.weight(.medium))
                            Text(server.baseURL).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if server.id == state.servers.selectedID { Image(systemName: "checkmark").foregroundStyle(Theme.gold) }
                    }
                    .contentShape(Rectangle())
                    .onTapGesture { if server.id != state.servers.selectedID { state.selectServer(server) } }
                    .contextMenu {
                        Button("编辑") { editor = ServerEditor(serverID: server.id, name: server.name, baseURL: server.baseURL) }
                        Button(role: .destructive) { state.removeServer(server.id) } label: { Label("删除", systemImage: "trash") }
                    }
                }
                Button { editor = ServerEditor(name: "", baseURL: "") } label: { Label("添加服务器", systemImage: "plus") }
            }

            Section("已保存账户") {
                if state.accountsForSelectedServer.isEmpty {
                    Text("当前服务器还没有保存账户").font(.caption).foregroundStyle(.secondary)
                }
                ForEach(state.accountsForSelectedServer) { account in
                    HStack {
                        Label(account.username, systemImage: "person.crop.circle")
                        Spacer()
                        Button("登录") { Task { _ = await state.quickLogin(account) } }.buttonStyle(.borderless)
                    }
                    .swipeActions {
                        Button(role: .destructive) {
                            state.accounts.remove(serverID: account.serverID, username: account.username)
                        } label: { Label("删除", systemImage: "trash") }
                    }
                }
            }

            Section("会话") {
                Button("退出登录", role: .destructive) { Task { await state.logout() } }
                Button("退出所有设备", role: .destructive) { Task { await state.logoutAll() } }
            }
        }
        .scrollContentBackground(.hidden)
        .screenBackground()
        .navigationTitle("服务器与账户")
        .sheet(item: $editor) { editor in
            let activate = editor.serverID == nil || editor.serverID == state.selectedServer?.id
            let previousURL = state.selectedServer?.baseURL
            ServerEditorSheet(editor: editor, store: state.servers) { server in
                state.reconcileServer(server, activate: activate, previousURL: previousURL)
            }
        }
    }
}
