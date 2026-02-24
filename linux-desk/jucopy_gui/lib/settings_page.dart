import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:yaru/yaru.dart';
import 'service_controller.dart';

class SettingsPage extends StatelessWidget {
  const SettingsPage({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Settings'),
      ),
      body: Consumer<ServiceController>(
        builder: (context, controller, child) {
          return ListView(
            padding: const EdgeInsets.all(24.0),
            children: [
              const Text(
                'General Settings',
                style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
              ),
              const SizedBox(height: 16),
              YaruSection(
                headline: const Text('Startup'),
                child: Column(
                  children: [
                    YaruCheckboxListTile(
                      value: controller.isEnabled,
                      onChanged: (value) async {
                        final success = await controller.toggleAutostart();
                        if (!success && context.mounted) {
                          ScaffoldMessenger.of(context).showSnackBar(
                            const SnackBar(content: Text('Failed to toggle autostart')),
                          );
                        }
                      },
                      title: const Text('Run at startup'),
                      subtitle: const Text('Automatically start the jucopy service on boot'),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 32),
              const Text(
                'About',
                style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
              ),
              const SizedBox(height: 16),
              const YaruSection(
                headline: Text('Information'),
                child: Column(
                  children: [
                    ListTile(
                      title: Text('JuCopy'),
                      subtitle: Text('Automatically sync PRIMARY selection to CLIPBOARD'),
                    ),
                    ListTile(
                      title: Text('Version'),
                      subtitle: Text('1.0.0'),
                    ),
                    ListTile(
                      title: Text('Engine'),
                      subtitle: Text('eBPF (uprobe)'),
                    ),
                  ],
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}
