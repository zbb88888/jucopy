import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:yaru/yaru.dart';
import 'service_controller.dart';
import 'dashboard_page.dart';
import 'settings_page.dart';

void main() {
  runApp(
    ChangeNotifierProvider(
      create: (_) => ServiceController(),
      child: const JuCopyApp(),
    ),
  );
}

class JuCopyApp extends StatelessWidget {
  const JuCopyApp({super.key});

  @override
  Widget build(BuildContext context) {
    return YaruTheme(
      builder: (context, yaru, child) {
        return MaterialApp(
          title: 'JuCopy',
          debugShowCheckedModeBanner: false,
          theme: yaru.theme,
          darkTheme: yaru.darkTheme,
          home: const HomePage(),
        );
      },
    );
  }
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  int _selectedIndex = 0;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Row(
        children: [
          NavigationRail(
            selectedIndex: _selectedIndex,
            onDestinationSelected: (index) {
              setState(() {
                _selectedIndex = index;
              });
            },
            labelType: NavigationRailLabelType.all,
            destinations: const [
              NavigationRailDestination(
                icon: Icon(Icons.dashboard_outlined),
                selectedIcon: Icon(Icons.dashboard),
                label: Text('Dashboard'),
              ),
              NavigationRailDestination(
                icon: Icon(Icons.settings_outlined),
                selectedIcon: Icon(Icons.settings),
                label: Text('Settings'),
              ),
            ],
          ),
          const VerticalDivider(thickness: 1, width: 1),
          Expanded(
            child: IndexedStack(
              index: _selectedIndex,
              children: const [
                DashboardPage(),
                SettingsPage(),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
