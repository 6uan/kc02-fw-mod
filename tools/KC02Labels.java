// KC02 Firmware Label Script for Ghidra
// Run via Script Manager after importing dump1.bin
//@category KC02

import ghidra.app.script.GhidraScript;
import ghidra.program.model.symbol.SourceType;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class KC02Labels extends GhidraScript {

    private Address addr(long offset) {
        return currentProgram.getAddressFactory().getDefaultAddressSpace().getAddress(offset);
    }

    private void label(long offset, String name) throws Exception {
        currentProgram.getSymbolTable().createLabel(addr(offset), name, SourceType.USER_DEFINED);
    }

    private void func(long offset, String name) throws Exception {
        Address a = addr(offset);
        Function f = currentProgram.getFunctionManager().getFunctionAt(a);
        if (f != null) {
            f.setName(name, SourceType.USER_DEFINED);
        } else {
            createFunction(a, name);
        }
    }

    @Override
    public void run() throws Exception {

        // ── Function Labels ──
        func(0x02003E74L, "handler_video");
        func(0x02003EF0L, "handler_music");
        func(0x02003F90L, "handler_playback");
        func(0x02004014L, "handler_games");
        func(0x02004098L, "handler_settings");
        func(0x0200411CL, "handler_camera");
        func(0x02006CBCL, "handler_state_17");
        func(0x02006CCCL, "handler_state_12");
        func(0x02006CE0L, "handler_state_14");
        func(0x02006CF8L, "handler_state_20");
        func(0x02006D00L, "handler_state_13");
        func(0x02006D10L, "handler_state_19");
        func(0x02006D58L, "handler_state_06");
        func(0x02006D60L, "handler_state_21");
        func(0x02006D6CL, "handler_state_22");
        func(0x02006D78L, "handler_state_32");
        func(0x02006D90L, "handler_state_33");
        func(0x02006DA8L, "handler_state_34");
        func(0x02006DC0L, "handler_state_27");
        func(0x02006DD4L, "handler_null_default");
        func(0x020093ACL, "init_callback_0");
        func(0x020093B4L, "init_callback_1");
        func(0x020093BCL, "init_callback_2");
        func(0x020093C4L, "init_callback_3");
        func(0x020093CCL, "init_callback_4");
        func(0x020093D4L, "init_callback_5");
        func(0x020093DCL, "init_callback_6");
        func(0x020093E4L, "init_callback_7");

        // ── Data Labels ──
        label(0x0207DE1CL, "str_battery");
        label(0x0207DE28L, "str_ad_key");
        label(0x0207DE2FL, "str_sd_card");
        label(0x0207DE42L, "str_cmos_sensor");
        label(0x0207DE52L, "str_video_path");
        label(0x0207DE59L, "str_photo_path");
        label(0x0207DE60L, "str_audio_path");
        label(0x0207DE87L, "str_destbin");
        label(0x0207DE93L, "str_upgrade_failed");
        label(0x0207DF08L, "ui_state_dispatch_table");
        setEOLComment(addr(0x0207DF08L), "35 x u32 function pointers");
        label(0x0207DF94L, "str_exmend_bin");
        label(0x0207DF9FL, "str_version");
        label(0x0207DFD8L, "str_build_date");
        label(0x0207DFE5L, "str_selftest_bin");
        label(0x0207E000L, "init_callback_table");
        setEOLComment(addr(0x0207E000L), "10 x u32 function pointers");
        label(0x0207E13CL, "str_chip_id");
        label(0x0207E143L, "str_firmware_version");
        label(0x0207E153L, "str_main_menu");
        label(0x0207E160L, "menu_coord_table");
        setEOLComment(addr(0x0207E160L), "6 x (u16 x, u16 y)");
        label(0x0207E178L, "menu_asset_index_table");
        setEOLComment(addr(0x0207E178L), "6 x u32 SFAT indices");
        label(0x0207E190L, "str_game_menu");
        label(0x0207E19CL, "mode_callback_table");
        setEOLComment(addr(0x0207E19CL), "5 x u32 function pointers");
        label(0x0207E1C4L, "str_audio_player");
        label(0x0207E1E4L, "str_setting_menu");
        label(0x020D3200L, "sfat_table");
        setEOLComment(addr(0x020D3200L), "97 x 8-byte asset entries");
        label(0x02239C5AL, "menu_label_settings");
        setEOLComment(addr(0x02239C5AL), "num_chars + string ref");
        label(0x02239C62L, "menu_label_video");
        setEOLComment(addr(0x02239C62L), "num_chars + string ref");
        label(0x02239C6AL, "menu_label_photo");
        setEOLComment(addr(0x02239C6AL), "num_chars + string ref");
        label(0x02239C72L, "menu_label_playback");
        setEOLComment(addr(0x02239C72L), "num_chars + string ref");
        label(0x02239C7AL, "menu_label_mp3");
        setEOLComment(addr(0x02239C7AL), "num_chars + string ref");
        label(0x02239C82L, "menu_label_games");
        setEOLComment(addr(0x02239C82L), "num_chars + string ref");

        // ── ADC Button Driver ──
        func(0x02002E0CL, "adc_init");
        func(0x02003A60L, "adc_handler");
        func(0x02004CC8L, "adc_key_scan");
        label(0x020C4C44L, "adc_driver_struct");
        setEOLComment(addr(0x020C4C44L), "ad-key driver: name, init, handler ptrs");
        label(0x020C4C80L, "adc_button_table");
        setEOLComment(addr(0x020C4C80L), "7 x 8B: [u32 rsv][u16 key_id][u16 adc_center]");
        label(0x020C4CBCL, "key_event_handlers");
        setEOLComment(addr(0x020C4CBCL), "19 x u32 function ptrs (3 per button)");

        println("KC02 labels applied: 31 functions, 34 data labels");
    }
}
