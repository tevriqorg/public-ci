package tech.bogomolov.incomingsmsgateway;

import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.net.ConnectivityManager;
import android.net.NetworkInfo;
import android.os.BatteryManager;
import android.os.Build;
import androidx.core.content.ContextCompat;
import androidx.work.Data;
import org.json.JSONObject;
import java.util.List;

/** Explicit opt-in E2EE latest-state snapshot, never an SMS inbox message. */
public final class P22DeviceStatus {
    private P22DeviceStatus() {}

    public static JSONObject capture(Context ctx) throws Exception {
        JSONObject value=new JSONObject();
        value.put("type","device_status");
        value.put("collectedAt",System.currentTimeMillis());
        value.put("model",Build.MANUFACTURER+" "+Build.MODEL);
        value.put("androidVersion",Build.VERSION.RELEASE);
        Intent battery=ctx.registerReceiver(null,new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        if(battery!=null){
            int level=battery.getIntExtra(BatteryManager.EXTRA_LEVEL,-1);
            int scale=battery.getIntExtra(BatteryManager.EXTRA_SCALE,-1);
            if(level>=0&&scale>0)value.put("battery",Math.round(level*100f/scale));
            int state=battery.getIntExtra(BatteryManager.EXTRA_STATUS,-1);
            value.put("charging",state==BatteryManager.BATTERY_STATUS_CHARGING
                    ||state==BatteryManager.BATTERY_STATUS_FULL);
            int temp=battery.getIntExtra(BatteryManager.EXTRA_TEMPERATURE,-1);
            if(temp>=0&&temp<=800)value.put("batteryCelsius",temp/10.0);
        }
        ConnectivityManager cm=(ConnectivityManager)ctx.getSystemService(Context.CONNECTIVITY_SERVICE);
        String network="offline";
        if(cm!=null){
            NetworkInfo info=cm.getActiveNetworkInfo();
            if(info!=null && info.isConnected())
                network=info.getType()==ConnectivityManager.TYPE_WIFI?"wifi":
                        info.getType()==ConnectivityManager.TYPE_MOBILE?"cellular":"other";
        }
        value.put("network",network);
        value.put("smsGranted",ContextCompat.checkSelfPermission(ctx,
                Manifest.permission.RECEIVE_SMS)==PackageManager.PERMISSION_GRANTED);
        value.put("notificationGranted",NotificationSettings.accessGranted(ctx));
        value.put("notificationForwarding",NotificationSettings.enabled(ctx));
        value.put("pendingFailures",FailedMessage.getCount(ctx));
        value.put("lastSmsAt",NotificationSettings.lastSms(ctx));
        value.put("lastNotificationAt",NotificationSettings.lastPost(ctx));
        value.put("lastDeliverySuccessAt",NotificationSettings.lastDeliverySuccess(ctx));
        boolean shizuku=ShizukuDiagnostics.binderAvailable();
        value.put("shizukuConnected",shizuku);
        boolean shizukuGranted=false;
        if(shizuku){
            try{
                shizukuGranted=rikka.shizuku.Shizuku.checkSelfPermission()
                        ==PackageManager.PERMISSION_GRANTED;
            }catch(RuntimeException ignored){/* May stop between checks. */}
        }
        value.put("shizukuGranted",shizukuGranted);
        // Do not read system-wide logs, third-party app content, IMEI or precise location.
        return value;
    }

    public static boolean enqueue(Context ctx) {
        if(!P22DeviceStatusSettings.enabled(ctx))return false;
        List<ForwardingConfig> configs=ForwardingConfig.getAll(ctx);
        for(ForwardingConfig config:configs) {
            if(!P22NotificationForwarder.isEligible(config))continue;
            try {
                JSONObject headers=new JSONObject(config.getHeaders());
                String device=headers.getString("X-PXT9-Recipient-Device");
                String publicKey=headers.getString("X-PXT9-Recipient-Key");
                String ciphertext=Pxt9Crypto.encryptStatus(
                    capture(ctx),device,publicKey);
                headers.remove("X-PXT9-Recipient-Device");
                headers.remove("X-PXT9-Recipient-Key");
                Data data=new Data.Builder()
                    .putString(RequestWorker.DATA_URL,"https://relay.p22.example.invalid/api/android-status")
                    .putString(RequestWorker.DATA_TEXT,ciphertext)
                    .putString(RequestWorker.DATA_HEADERS,headers.toString())
                    .putBoolean(RequestWorker.DATA_PXT9_MODE,true)
                    .putBoolean(RequestWorker.DATA_DEVICE_STATUS,true)
                    .putBoolean(RequestWorker.DATA_IGNORE_SSL,false)
                    .putBoolean(RequestWorker.DATA_CHUNKED_MODE,false)
                    .putInt(RequestWorker.DATA_MAX_RETRIES,4)
                    .putString(RequestWorker.DATA_SIGN_HMAC_SHA256_SECRET,
                        config.getSignHmacSha256Secret())
                    .putBoolean(RequestWorker.DATA_STORE_FAILED,false)
                    .build();
                RequestWorker.enqueue(ctx,data);
                P22DeviceStatusSettings.record(ctx,"queued");
                return true;
            }catch(Exception ignored){
                P22DeviceStatusSettings.record(ctx,"prepare_failed");
                // Never log device identifiers, signed headers or encrypted payload.
                return false;
            }
        }
        P22DeviceStatusSettings.record(ctx,"no_rule");
        return false;
    }
}
