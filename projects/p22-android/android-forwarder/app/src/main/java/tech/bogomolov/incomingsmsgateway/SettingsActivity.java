package tech.bogomolov.incomingsmsgateway;

import android.Manifest;
import android.app.ActivityManager;
import android.content.Context;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.provider.Telephony;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.format.DateFormat;
import android.widget.TextView;
import androidx.core.content.ContextCompat;
import java.util.Date;
import java.util.Locale;

import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.TextUtils;
import android.util.Log;
import android.view.MenuItem;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.Toast;

import androidx.appcompat.app.AlertDialog;
import androidx.appcompat.app.ActionBar;
import androidx.appcompat.app.AppCompatActivity;
import androidx.appcompat.widget.SwitchCompat;
import rikka.shizuku.Shizuku;

import org.json.JSONException;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.MalformedURLException;
import java.net.URL;
import java.nio.charset.Charset;

/**
 * Global app settings. Currently hosts the heartbeat / app-alive monitor
 * (issue #31); the heartbeat itself runs in {@link SmsReceiverService}.
 */
public class SettingsActivity extends AppCompatActivity {

    // Backup export/import (issue #76). Uses the Storage Access Framework, which is
    // API 19+; the buttons hosting these flows are hidden on older devices.
    private static final int REQUEST_EXPORT = 1;
    private static final int REQUEST_IMPORT = 2;
    private static final int REQUEST_SHIZUKU_PERMISSION = 2206;
    private final Shizuku.OnRequestPermissionResultListener shizukuPermissionListener =
            (requestCode, grantResult) -> {
                if (requestCode == REQUEST_SHIZUKU_PERMISSION) {
                    runOnUiThread(() -> {
                        refreshP22Diagnostics();
                        Toast.makeText(this,
                                grantResult == PackageManager.PERMISSION_GRANTED
                                        ? R.string.p22_shizuku_granted
                                        : R.string.p22_shizuku_denied,
                                Toast.LENGTH_SHORT).show();
                    });
                }
            };
    private static final String BACKUP_FILENAME = "sms-gateway-backup.json";
    private static final String BACKUP_MIME_TYPE = "application/json";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_settings);

        ActionBar actionBar = getSupportActionBar();
        if (actionBar != null) {
            actionBar.setDisplayHomeAsUpEnabled(true);
        }

        HeartbeatSettings settings = HeartbeatSettings.load(this);

        final SwitchCompat enabledCheckbox = findViewById(R.id.input_heartbeat_enabled);
        enabledCheckbox.setChecked(settings.isEnabled());

        final EditText urlInput = findViewById(R.id.input_heartbeat_url);
        urlInput.setText(settings.getUrl());

        final EditText intervalInput = findViewById(R.id.input_heartbeat_interval);
        intervalInput.setText(String.valueOf(settings.getIntervalMinutes()));

        Button saveButton = findViewById(R.id.btn_heartbeat_save);
        saveButton.setOnClickListener(v -> {
            HeartbeatSettings updated = readSettings();
            if (updated == null) {
                return;
            }
            updated.save(this);
            applyHeartbeat(updated);
            Toast.makeText(this, R.string.heartbeat_saved_toast, Toast.LENGTH_SHORT).show();
            finish();
        });

        Button testButton = findViewById(R.id.btn_heartbeat_test);
        testButton.setOnClickListener(v -> testHeartbeat());

        setupBackupSection();
        setupNotificationSection();
        setupDeviceStatusSection();
        Shizuku.addRequestPermissionResultListener(shizukuPermissionListener);
    }

    @Override
    protected void onDestroy() {
        Shizuku.removeRequestPermissionResultListener(shizukuPermissionListener);
        super.onDestroy();
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshP22Diagnostics();
    }

    private void setupNotificationSection() {
        SwitchCompat enabled = findViewById(R.id.input_forward_notifications);
        enabled.setChecked(NotificationSettings.enabled(this));
        EditText allowed = findViewById(R.id.input_allowed_packages);
        EditText blocked = findViewById(R.id.input_blocked_packages);
        allowed.setText(NotificationSettings.allowed(this));
        blocked.setText(NotificationSettings.blocked(this));

        findViewById(R.id.btn_notification_access).setOnClickListener(view -> {
            try {
                startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS));
            } catch (Exception ignored) {
                openSystemAppSettings();
            }
        });
        findViewById(R.id.btn_p22_app_settings)
                .setOnClickListener(view -> openSystemAppSettings());
        findViewById(R.id.btn_p22_shizuku_permission)
                .setOnClickListener(view -> requestShizukuPermission());
        findViewById(R.id.btn_select_allowed_apps)
                .setOnClickListener(view -> selectAppSources(allowed, true));
        findViewById(R.id.btn_select_blocked_apps)
                .setOnClickListener(view -> selectAppSources(blocked, false));
        findViewById(R.id.btn_notifications_save).setOnClickListener(view -> {
            String allowedPackages = allowed.getText().toString();
            String blockedPackages = blocked.getText().toString();
            if (!validPackages(allowedPackages)) {
                allowed.setError("包名格式错误，请使用如 com.example.app 的格式");
                return;
            }
            if (!validPackages(blockedPackages)) {
                blocked.setError("包名格式错误，请使用如 com.example.app 的格式");
                return;
            }
            Runnable save = () -> {
                NotificationSettings.save(this, enabled.isChecked(),
                        blockedPackages, allowedPackages);
                Toast.makeText(this, R.string.p22_notifications_saved,
                        Toast.LENGTH_SHORT).show();
                refreshP22Diagnostics();
            };
            if (enabled.isChecked() && !NotificationSettings.enabled(this)) {
                new AlertDialog.Builder(this)
                        .setTitle(R.string.p22_notification_permission_warning_title)
                        .setMessage(R.string.p22_notification_permission_warning)
                        .setPositiveButton(R.string.btn_save,
                                (dialog, which) -> save.run())
                        .setNegativeButton(R.string.btn_cancel, null).show();
            } else {
                save.run();
            }
        });
    }

    private void setupDeviceStatusSection() {
        SwitchCompat monitor=findViewById(R.id.input_device_status);
        monitor.setChecked(P22DeviceStatusSettings.enabled(this));
        findViewById(R.id.btn_device_status_send).setOnClickListener(view -> {
            if(!P22DeviceStatusSettings.enabled(this)){
                Toast.makeText(this,R.string.p22_device_status_first_enable,Toast.LENGTH_LONG).show();
                return;
            }
            boolean queued=P22DeviceStatus.enqueue(this);
            refreshP22Diagnostics();
            Toast.makeText(this,queued?R.string.p22_device_status_queued:
                R.string.p22_device_status_queue_failed,Toast.LENGTH_LONG).show();
        });
        findViewById(R.id.btn_device_status_save).setOnClickListener(view -> {
            Runnable apply=()->{
                P22DeviceStatusSettings.setEnabled(this,monitor.isChecked());
                Intent intent=new Intent(this,SmsReceiverService.class);
                intent.setAction("tech.bogomolov.incomingsmsgateway.RESCHEDULE_STATUS");
                startService(intent);
                if(monitor.isChecked())P22DeviceStatus.enqueue(this);
                refreshP22Diagnostics();
                Toast.makeText(this,R.string.p22_device_status_saved,Toast.LENGTH_SHORT).show();
            };
            if(monitor.isChecked()&&!P22DeviceStatusSettings.enabled(this)) {
                new AlertDialog.Builder(this)
                    .setTitle(R.string.p22_device_status_title)
                    .setMessage(R.string.p22_device_status_consent)
                    .setPositiveButton(R.string.btn_save,(dialog,which)->apply.run())
                    .setNegativeButton(R.string.btn_cancel,null).show();
            }else apply.run();
        });
    }

    /** Searchable, icon-backed source selection; advanced manual input remains available. */
    private void selectAppSources(EditText target, boolean allowedOnly) {
        P22AppSourcePicker.show(this, target, allowedOnly);
    }

    private static boolean validPackages(String packages) {
        if (packages == null || packages.trim().isEmpty()) return true;
        if (packages.length() > 4000) return false;
        for (String pkg : packages.split("[,;\\s]+")) {
            if (!pkg.trim().isEmpty()
                    && !pkg.trim().matches("[A-Za-z0-9_]+(\\.[A-Za-z0-9_]+)+"))
                return false;
        }
        return true;
    }

    /** Never requests a privileged operation; only the official Shizuku opt-in. */
    private void requestShizukuPermission() {
        if (!ShizukuDiagnostics.binderAvailable()) {
            Toast.makeText(this, R.string.p22_shizuku_not_running,
                    Toast.LENGTH_LONG).show();
            refreshP22Diagnostics();
            return;
        }
        try {
            if (Shizuku.isPreV11()) {
                Toast.makeText(this, R.string.p22_shizuku_old_version,
                        Toast.LENGTH_LONG).show();
            } else if (Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED) {
                Toast.makeText(this, R.string.p22_shizuku_granted,
                        Toast.LENGTH_SHORT).show();
            } else if (Shizuku.shouldShowRequestPermissionRationale()) {
                Toast.makeText(this, R.string.p22_shizuku_denied,
                        Toast.LENGTH_LONG).show();
            } else {
                new AlertDialog.Builder(this)
                        .setTitle(R.string.p22_shizuku_consent_title)
                        .setMessage(R.string.p22_shizuku_consent_body)
                        .setPositiveButton(R.string.p22_shizuku_request,
                                (dialog, which) -> {
                                    try { Shizuku.requestPermission(REQUEST_SHIZUKU_PERMISSION); }
                                    catch (RuntimeException ignored) {
                                        Toast.makeText(this, R.string.p22_shizuku_not_running,
                                                Toast.LENGTH_LONG).show();
                                    }
                                })
                        .setNegativeButton(R.string.btn_cancel, null).show();
            }
        } catch (RuntimeException ignored) {
            Toast.makeText(this, R.string.p22_shizuku_not_running,
                    Toast.LENGTH_LONG).show();
        }
        refreshP22Diagnostics();
    }

    private void openSystemAppSettings() {
        Intent intent = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                Uri.parse("package:" + getPackageName()));
        startActivity(intent);
    }

    private static String when(long value) {
        return value <= 0 ? "尚无记录" : DateFormat.format("MM-dd HH:mm:ss", new Date(value)).toString();
    }

    private void refreshP22Diagnostics() {
        TextView status = findViewById(R.id.p22_diagnostics);
        if (status == null) return;
        boolean smsGranted = ContextCompat.checkSelfPermission(
                this, Manifest.permission.RECEIVE_SMS) == PackageManager.PERMISSION_GRANTED;
        boolean notificationGranted = NotificationSettings.accessGranted(this);
        boolean hasRule = P22NotificationForwarder.hasEligibleRule(this);
        PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
        boolean optimized = power != null && !power.isIgnoringBatteryOptimizations(getPackageName());
        String report = "接收短信权限：" + (smsGranted ? "允许" : "未授权")
                + "\n通知使用权：" + (notificationGranted ? "已授权" : "未授权")
                + "\nAPP 通知转发：" + (NotificationSettings.enabled(this) ? "开启" : "关闭")
                + "\n东京加密规则：" + (hasRule ? "存在已开启规则" : "无已开启规则")
                + "\n后台电池优化：" + (optimized ? "可能限制后台" : "系统电池优化已豁免")
                + "\n待重发失败记录：" + FailedMessage.getCount(this)
                + "\n最近 SMS 广播：" + when(NotificationSettings.lastSms(this))
                + "\n最近监听服务连接：" + when(NotificationSettings.lastListenerConnected(this))
                + "\n最近 APP 通知回调：" + when(NotificationSettings.lastPost(this))
                + "\n最近通知加密排队：" + when(NotificationSettings.lastEnqueued(this))
                + "\n最近东京发送尝试：" + when(NotificationSettings.lastDeliveryAttempt(this))
                + "\n最近东京发送成功：" + when(NotificationSettings.lastDeliverySuccess(this))
                + "\n最近东京发送失败：" + when(NotificationSettings.lastDeliveryFailure(this))
                + "\nShizuku：" + ShizukuDiagnostics.status(this)
                + "\n设备状态加密同步：" + (P22DeviceStatusSettings.enabled(this) ? "已开启" : "关闭")
                + "\n状态最近排队：" + when(P22DeviceStatusSettings.last(this,"queued"))
                + "\n状态最近送达成功：" + when(P22DeviceStatusSettings.last(this,"success"))
                + "\n状态最近发送失败：" + when(P22DeviceStatusSettings.last(this,"failure"))
                + "\n规则不匹配：" + when(P22DeviceStatusSettings.last(this,"no_rule"))
                + "\n状态准备失败：" + when(P22DeviceStatusSettings.last(this,"prepare_failed"));
        status.setText(report);
    }

    // The Storage Access Framework intents used for backup require API 19+; hide
    // the whole section on older devices rather than offer a button that can't
    // resolve an activity.
    private void setupBackupSection() {
        View backupSection = findViewById(R.id.backup_section);
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.KITKAT) {
            backupSection.setVisibility(View.GONE);
            return;
        }

        findViewById(R.id.btn_export).setOnClickListener(v -> confirmExport());
        findViewById(R.id.btn_import).setOnClickListener(v -> startImport());
    }

    // Warn before exporting: the file holds webhook URLs, custom headers and HMAC
    // secrets in plain text. Only on confirmation do we open the file picker.
    private void confirmExport() {
        new AlertDialog.Builder(this)
                .setTitle(R.string.backup_export_warning_title)
                .setMessage(R.string.backup_export_warning_message)
                .setPositiveButton(R.string.btn_export, (dialog, which) -> startExport())
                .setNegativeButton(R.string.btn_cancel, null)
                .show();
    }

    private void startExport() {
        Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType(BACKUP_MIME_TYPE);
        intent.putExtra(Intent.EXTRA_TITLE, BACKUP_FILENAME);
        startActivityForResult(intent, REQUEST_EXPORT);
    }

    private void startImport() {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        // Some file managers tag .json files as octet-stream, so accept any type
        // and let the JSON parser reject a wrong file.
        intent.setType("*/*");
        startActivityForResult(intent, REQUEST_IMPORT);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (resultCode != RESULT_OK || data == null || data.getData() == null) {
            return;
        }
        Uri uri = data.getData();
        if (requestCode == REQUEST_EXPORT) {
            exportTo(uri);
        } else if (requestCode == REQUEST_IMPORT) {
            importFrom(uri);
        }
    }

    private void exportTo(Uri uri) {
        try (OutputStream output = getContentResolver().openOutputStream(uri)) {
            String content = ForwardingConfig.exportToJson(this);
            output.write(content.getBytes(Charset.forName("UTF-8")));
            int count = ForwardingConfig.getAll(this).size();
            Toast.makeText(this, getString(R.string.backup_export_success, count),
                    Toast.LENGTH_LONG).show();
        } catch (IOException | JSONException e) {
            Log.e("SettingsActivity", "export failed: " + e);
            Toast.makeText(this, R.string.backup_export_failed, Toast.LENGTH_LONG).show();
        }
    }

    private void importFrom(Uri uri) {
        try (InputStream input = getContentResolver().openInputStream(uri)) {
            String content = readStream(input);
            int count = ForwardingConfig.importFromJson(this, content);
            Toast.makeText(this, getString(R.string.backup_import_success, count),
                    Toast.LENGTH_LONG).show();
            // New rules need the SMS-listening service running, just as adding one
            // through MainActivity would.
            startServiceIfNeeded();
        } catch (IOException | JSONException e) {
            Log.e("SettingsActivity", "import failed: " + e);
            Toast.makeText(this, R.string.backup_import_failed, Toast.LENGTH_LONG).show();
        }
    }

    private String readStream(InputStream input) throws IOException {
        BufferedReader reader = new BufferedReader(
                new InputStreamReader(input, Charset.forName("UTF-8")));
        StringBuilder builder = new StringBuilder();
        String line;
        while ((line = reader.readLine()) != null) {
            builder.append(line).append('\n');
        }
        return builder.toString();
    }

    // Starts the foreground SMS service if it isn't already running, so imported
    // rules take effect without a restart. Mirrors MainActivity.startService().
    private void startServiceIfNeeded() {
        if (isServiceRunning()) {
            return;
        }
        Intent intent = new Intent(this, SmsReceiverService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            getApplicationContext().startForegroundService(intent);
        } else {
            getApplicationContext().startService(intent);
        }
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        if (item.getItemId() == android.R.id.home) {
            finish();
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    // Validates the form and returns the entered settings, or null (with an inline
    // error set) when invalid. URL/interval are only enforced when the heartbeat is
    // enabled, so a user can clear the toggle without filling a valid URL first.
    private HeartbeatSettings readSettings() {
        final SwitchCompat enabledCheckbox = findViewById(R.id.input_heartbeat_enabled);
        boolean enabled = enabledCheckbox.isChecked();

        final EditText urlInput = findViewById(R.id.input_heartbeat_url);
        String url = urlInput.getText().toString().trim();

        final EditText intervalInput = findViewById(R.id.input_heartbeat_interval);
        String intervalText = intervalInput.getText().toString().trim();
        int interval = intervalText.isEmpty() ? 0 : Integer.parseInt(intervalText);

        if (enabled) {
            if (TextUtils.isEmpty(url)) {
                urlInput.setError(getString(R.string.error_empty_url));
                return null;
            }
            try {
                new URL(url);
            } catch (MalformedURLException e) {
                urlInput.setError(getString(R.string.error_wrong_url));
                return null;
            }
            if (interval < HeartbeatSettings.MIN_INTERVAL_MINUTES) {
                intervalInput.setError(getString(R.string.error_wrong_interval));
                return null;
            }
        } else if (interval < HeartbeatSettings.MIN_INTERVAL_MINUTES) {
            interval = HeartbeatSettings.DEFAULT_INTERVAL_MINUTES;
        }

        return new HeartbeatSettings(enabled, url, interval);
    }

    // Pushes the new settings to the service. Starting it with the reschedule action
    // both (re)schedules a running service and starts one if needed (e.g. heartbeat
    // turned on while no forwarding rules exist). When disabling with no service
    // running, there is nothing to do — don't spin one up just to stop the ping.
    private void applyHeartbeat(HeartbeatSettings settings) {
        if (!settings.isEnabled() && !isServiceRunning()) {
            return;
        }

        Intent intent = new Intent(this, SmsReceiverService.class);
        intent.setAction(SmsReceiverService.ACTION_RESCHEDULE_HEARTBEAT);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            getApplicationContext().startForegroundService(intent);
        } else {
            getApplicationContext().startService(intent);
        }
    }

    private void testHeartbeat() {
        final EditText urlInput = findViewById(R.id.input_heartbeat_url);
        String url = urlInput.getText().toString().trim();
        if (TextUtils.isEmpty(url)) {
            urlInput.setError(getString(R.string.error_empty_url));
            return;
        }
        try {
            new URL(url);
        } catch (MalformedURLException e) {
            urlInput.setError(getString(R.string.error_wrong_url));
            return;
        }

        new Thread(() -> {
            Request request = new Request(url, "");
            request.setUseChunkedMode(false);
            String result = request.execute();
            runOnUiThread(() ->
                    Toast.makeText(getApplicationContext(), result, Toast.LENGTH_LONG).show());
        }).start();
    }

    private boolean isServiceRunning() {
        ActivityManager manager = (ActivityManager) getSystemService(Context.ACTIVITY_SERVICE);
        for (ActivityManager.RunningServiceInfo service : manager.getRunningServices(Integer.MAX_VALUE)) {
            if (SmsReceiverService.class.getName().equals(service.service.getClassName())) {
                return true;
            }
        }
        return false;
    }
}
