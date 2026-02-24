import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:yaru/yaru.dart';
import 'service_controller.dart';

class DashboardPage extends StatelessWidget {
  const DashboardPage({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Dashboard'),
      ),
      body: Consumer<ServiceController>(
        builder: (context, controller, child) {
          return Padding(
            padding: const EdgeInsets.all(24.0),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                _buildStatusCard(context, controller),
                const SizedBox(height: 32),
                const Text(
                  'Quick Actions',
                  style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
                ),
                const SizedBox(height: 16),
                Wrap(
                  spacing: 16,
                  runSpacing: 16,
                  children: [
                    ElevatedButton.icon(
                      onPressed: controller.isChecking ? null : controller.refreshStatus,
                      icon: const Icon(Icons.refresh),
                      label: const Text('Refresh Status'),
                    ),
                  ],
                ),
                if (controller.lastError.isNotEmpty) ...[
                  const SizedBox(height: 32),
                  const Text(
                    'Last Error',
                    style: TextStyle(color: Colors.red, fontWeight: FontWeight.bold),
                  ),
                  const SizedBox(height: 8),
                  Container(
                    padding: const EdgeInsets.all(12),
                    decoration: BoxDecoration(
                      color: Colors.red.withOpacity(0.1),
                      borderRadius: BorderRadius.circular(8),
                      border: Border.all(color: Colors.red.withOpacity(0.3)),
                    ),
                    child: Text(
                      controller.lastError,
                      style: const TextStyle(fontFamily: 'monospace', fontSize: 12),
                    ),
                  ),
                ],
              ],
            ),
          );
        },
      ),
    );
  }

  Widget _buildStatusCard(BuildContext context, ServiceController controller) {
    Color statusColor;
    String statusText;
    IconData statusIcon;

    switch (controller.status) {
      case ServiceStatus.active:
        statusColor = Colors.green;
        statusText = 'Active (Running)';
        statusIcon = Icons.check_circle;
        break;
      case ServiceStatus.inactive:
        statusColor = Colors.grey;
        statusText = 'Inactive (Stopped)';
        statusIcon = Icons.pause_circle_filled;
        break;
      case ServiceStatus.failed:
        statusColor = Colors.red;
        statusText = 'Failed';
        statusIcon = Icons.error;
        break;
      case ServiceStatus.unknown:
      default:
        statusColor = Colors.orange;
        statusText = 'Unknown';
        statusIcon = Icons.help;
        break;
    }

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(20.0),
        child: Row(
          children: [
            Icon(statusIcon, size: 48, color: statusColor),
            const SizedBox(width: 20),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                    'Service Status',
                    style: TextStyle(fontSize: 14, color: Colors.grey),
                  ),
                  Text(
                    statusText,
                    style: TextStyle(
                      fontSize: 24,
                      fontWeight: FontWeight.bold,
                      color: statusColor,
                    ),
                  ),
                ],
              ),
            ),
            YaruSwitch(
              value: controller.status == ServiceStatus.active,
              onChanged: (value) async {
                final success = await controller.toggleService();
                if (!success && context.mounted) {
                  ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(content: Text('Failed to toggle service')),
                  );
                }
              },
            ),
          ],
        ),
      ),
    );
  }
}
