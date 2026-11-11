package tech.bogomolov.incomingsmsgateway;

import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.provider.Telephony;
import android.text.Editable;
import android.text.TextWatcher;
import android.text.TextUtils;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.ListView;
import android.widget.TextView;
import android.widget.Toast;
import androidx.appcompat.app.AlertDialog;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;

/**
 * A local-only, searchable app picker. Only launcher-visible apps and the
 * current default SMS app are enumerated; no broad package inventory grant.
 */
public final class P22AppSourcePicker {
    private P22AppSourcePicker() { }

    private static final class Entry {
        final String pkg;
        final String label;
        Entry(String pkg, String label) { this.pkg = pkg; this.label = label; }
    }

    public static void show(Context context, EditText target, boolean allowedOnly) {
        PackageManager pm = context.getPackageManager();
        Map<String, String> names = new TreeMap<>();
        Intent launchers = new Intent(Intent.ACTION_MAIN);
        launchers.addCategory(Intent.CATEGORY_LAUNCHER);
        try {
            for (ResolveInfo app : pm.queryIntentActivities(launchers, 0)) {
                if (app == null || app.activityInfo == null) continue;
                String pkg = app.activityInfo.packageName;
                if (!NotificationPolicy.parsePackages(pkg).contains(pkg)) continue;
                CharSequence label = app.loadLabel(pm);
                names.put(pkg, label == null ? pkg : label.toString());
            }
        } catch (RuntimeException ignored) {
            // Some OEMs restrict package discovery; retain manual entry fallback.
        }
        try {
            String sms = Telephony.Sms.getDefaultSmsPackage(context);
            if (sms != null && NotificationPolicy.parsePackages(sms).contains(sms)) {
                String label = sms;
                try { label = pm.getApplicationLabel(pm.getApplicationInfo(sms, 0)).toString(); }
                catch (RuntimeException | PackageManager.NameNotFoundException ignored) { }
                names.put(sms, label + "（默认短信）");
            }
        } catch (RuntimeException ignored) { }
        if (names.isEmpty()) {
            Toast.makeText(context, R.string.p22_pick_empty, Toast.LENGTH_LONG).show();
            return;
        }

        List<Entry> all = new ArrayList<>();
        for (Map.Entry<String, String> app : names.entrySet())
            all.add(new Entry(app.getKey(), app.getValue()));
        all.sort(Comparator.comparing(a -> a.label.toLowerCase(Locale.ROOT)));
        Set<String> previous = NotificationPolicy.parsePackages(target.getText().toString());
        Set<String> selected = new HashSet<>(previous);
        List<Entry> visible = new ArrayList<>(all);

        View root = LayoutInflater.from(context).inflate(R.layout.p22_app_source_picker, null);
        EditText search = root.findViewById(R.id.p22_picker_search);
        ListView list = root.findViewById(R.id.p22_picker_list);
        TextView empty = root.findViewById(R.id.p22_picker_empty);
        list.setEmptyView(empty);
        BaseAdapter adapter = new BaseAdapter() {
            @Override public int getCount() { return visible.size(); }
            @Override public Entry getItem(int position) { return visible.get(position); }
            @Override public long getItemId(int position) { return position; }
            @Override public View getView(int position, View convertView, ViewGroup parent) {
                View row = convertView == null
                        ? LayoutInflater.from(context).inflate(R.layout.p22_app_source_item, parent, false)
                        : convertView;
                Entry item = getItem(position);
                ((TextView) row.findViewById(R.id.p22_item_label)).setText(item.label);
                ((TextView) row.findViewById(R.id.p22_item_package)).setText(item.pkg);
                ImageView icon = row.findViewById(R.id.p22_item_icon);
                try { icon.setImageDrawable(pm.getApplicationIcon(item.pkg)); }
                catch (PackageManager.NameNotFoundException ignored) {
                    icon.setImageDrawable(pm.getDefaultActivityIcon());
                }
                ((CheckBox) row.findViewById(R.id.p22_item_checked))
                        .setChecked(selected.contains(item.pkg));
                return row;
            }
        };
        list.setAdapter(adapter);
        list.setOnItemClickListener((parent, view, position, id) -> {
            String pkg = visible.get(position).pkg;
            if (!selected.add(pkg)) selected.remove(pkg);
            adapter.notifyDataSetChanged();
        });
        search.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int start, int count, int after) { }
            @Override public void onTextChanged(CharSequence s, int start, int before, int count) {
                visible.clear();
                for (Entry item : all)
                    if (AppSourceSearch.matches(item.label, item.pkg, s.toString())) visible.add(item);
                adapter.notifyDataSetChanged();
            }
            @Override public void afterTextChanged(Editable editable) { }
        });
        new AlertDialog.Builder(context)
                .setTitle(allowedOnly ? R.string.p22_pick_allowed_title : R.string.p22_pick_blocked_title)
                .setView(root)
                .setPositiveButton(R.string.p22_pick_apply, (dialog, which) -> {
                    Set<String> updated = new TreeSet<>(previous);
                    for (Entry entry : all) updated.remove(entry.pkg);
                    for (Entry entry : all) if (selected.contains(entry.pkg)) updated.add(entry.pkg);
                    if (allowedOnly && updated.isEmpty()) {
                        Toast.makeText(context, R.string.p22_pick_choose_one, Toast.LENGTH_LONG).show();
                        return;
                    }
                    target.setText(TextUtils.join(", ", updated));
                    Toast.makeText(context, R.string.p22_pick_hint, Toast.LENGTH_LONG).show();
                })
                .setNegativeButton(R.string.btn_cancel, null)
                .show();
    }
}
