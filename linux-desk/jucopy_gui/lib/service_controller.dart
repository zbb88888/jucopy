import 'dart:async';
import 'dart:io';
import 'package:flutter/foundation.dart';

enum ServiceStatus {
  active,
  inactive,
  failed,
  unknown,
}

class ServiceController extends ChangeNotifier {
  ServiceStatus _status = ServiceStatus.unknown;
  bool _isEnabled = false;
  bool _isChecking = false;
  String _lastError = '';

  ServiceStatus get status => _status;
  bool get isEnabled => _isEnabled;
  bool get isChecking => _isChecking;
  String get lastError => _lastError;

  ServiceController() {
    refreshStatus();
    // Periodically refresh status
    Timer.periodic(const Duration(seconds: 5), (_) => refreshStatus());
  }

  Future<void> refreshStatus() async {
    _isChecking = true;
    notifyListeners();

    try {
      _status = await _checkActiveStatus();
      _isEnabled = await _checkEnabledStatus();
      _lastError = '';
    } catch (e) {
      _lastError = e.toString();
      _status = ServiceStatus.unknown;
    } finally {
      _isChecking = false;
      notifyListeners();
    }
  }

  Future<ServiceStatus> _checkActiveStatus() async {
    final result = await Process.run('systemctl', ['is-active', 'jucopy']);
    final output = result.stdout.toString().trim();

    switch (output) {
      case 'active':
        return ServiceStatus.active;
      case 'inactive':
        return ServiceStatus.inactive;
      case 'failed':
        return ServiceStatus.failed;
      default:
        return ServiceStatus.unknown;
    }
  }

  Future<bool> _checkEnabledStatus() async {
    final result = await Process.run('systemctl', ['is-enabled', 'jucopy']);
    final output = result.stdout.toString().trim();
    return output == 'enabled';
  }

  Future<bool> toggleService() async {
    final action = (_status == ServiceStatus.active) ? 'stop' : 'start';
    return _runCommandWithPkexec(['systemctl', action, 'jucopy']);
  }

  Future<bool> toggleAutostart() async {
    final action = _isEnabled ? 'disable' : 'enable';
    final success = await _runCommandWithPkexec(['systemctl', action, 'jucopy']);
    if (success) {
      _isEnabled = !_isEnabled;
      notifyListeners();
    }
    return success;
  }

  Future<bool> _runCommandWithPkexec(List<String> command) async {
    try {
      final result = await Process.run('pkexec', command);
      if (result.exitCode == 0) {
        await refreshStatus();
        return true;
      } else {
        _lastError = result.stderr.toString();
        notifyListeners();
        return false;
      }
    } catch (e) {
      _lastError = e.toString();
      notifyListeners();
      return false;
    }
  }
}
