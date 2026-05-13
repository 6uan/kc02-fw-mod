# Auto-generated Ghidra script for KC02 firmware
# Run via Script Manager after importing dump1.bin
#@category KC02

from ghidra.program.model.symbol import SourceType
from ghidra.program.model.address import AddressFactory

fm = currentProgram.getFunctionManager()
st = currentProgram.getSymbolTable()
af = currentProgram.getAddressFactory()
space = af.getDefaultAddressSpace()

def addr(offset):
    return space.getAddress(offset)

def label(offset, name):
    st.createLabel(addr(offset), name, SourceType.USER_DEFINED)

def func(offset, name):
    a = addr(offset)
    f = fm.getFunctionAt(a)
    if f:
        f.setName(name, SourceType.USER_DEFINED)
    else:
        createFunction(a, name)

# ── Function Labels ──
func(0x02003E74, "handler_video")
func(0x02003EF0, "handler_music")
func(0x02003F90, "handler_playback")
func(0x02004014, "handler_games")
func(0x02004098, "handler_settings")
func(0x0200411C, "handler_camera")
func(0x02006CBC, "handler_state_17")
func(0x02006CCC, "handler_state_12")
func(0x02006CE0, "handler_state_14")
func(0x02006CF8, "handler_state_20")
func(0x02006D00, "handler_state_13")
func(0x02006D10, "handler_state_19")
func(0x02006D58, "handler_state_06")
func(0x02006D60, "handler_state_21")
func(0x02006D6C, "handler_state_22")
func(0x02006D78, "handler_state_32")
func(0x02006D90, "handler_state_33")
func(0x02006DA8, "handler_state_34")
func(0x02006DC0, "handler_state_27")
func(0x02006DD4, "handler_null_default")
func(0x020093AC, "init_callback_0")
func(0x020093B4, "init_callback_1")
func(0x020093BC, "init_callback_2")
func(0x020093C4, "init_callback_3")
func(0x020093CC, "init_callback_4")
func(0x020093D4, "init_callback_5")
func(0x020093DC, "init_callback_6")
func(0x020093E4, "init_callback_7")

# ── Data Labels ──
label(0x0207DE1C, "str_battery")
label(0x0207DE28, "str_ad_key")
label(0x0207DE2F, "str_sd_card")
label(0x0207DE42, "str_cmos_sensor")
label(0x0207DE52, "str_video_path")
label(0x0207DE59, "str_photo_path")
label(0x0207DE60, "str_audio_path")
label(0x0207DE87, "str_destbin")
label(0x0207DE93, "str_upgrade_failed")
label(0x0207DF08, "ui_state_dispatch_table")
setEOLComment(addr(0x0207DF08), "35 x u32 function pointers")
label(0x0207DF94, "str_exmend_bin")
label(0x0207DF9F, "str_version")
label(0x0207DFD8, "str_build_date")
label(0x0207DFE5, "str_selftest_bin")
label(0x0207E000, "init_callback_table")
setEOLComment(addr(0x0207E000), "10 x u32 function pointers")
label(0x0207E13C, "str_chip_id")
label(0x0207E143, "str_firmware_version")
label(0x0207E153, "str_main_menu")
label(0x0207E160, "menu_coord_table")
setEOLComment(addr(0x0207E160), "6 x (u16 x, u16 y)")
label(0x0207E178, "menu_asset_index_table")
setEOLComment(addr(0x0207E178), "6 x u32 SFAT indices")
label(0x0207E190, "str_game_menu")
label(0x0207E19C, "mode_callback_table")
setEOLComment(addr(0x0207E19C), "5 x u32 function pointers")
label(0x0207E1C4, "str_audio_player")
label(0x0207E1E4, "str_setting_menu")
label(0x020D3200, "sfat_table")
setEOLComment(addr(0x020D3200), "97 x 8-byte asset entries")
label(0x02239C5A, "menu_label_settings")
setEOLComment(addr(0x02239C5A), "num_chars + string ref")
label(0x02239C62, "menu_label_video")
setEOLComment(addr(0x02239C62), "num_chars + string ref")
label(0x02239C6A, "menu_label_photo")
setEOLComment(addr(0x02239C6A), "num_chars + string ref")
label(0x02239C72, "menu_label_playback")
setEOLComment(addr(0x02239C72), "num_chars + string ref")
label(0x02239C7A, "menu_label_mp3")
setEOLComment(addr(0x02239C7A), "num_chars + string ref")
label(0x02239C82, "menu_label_games")
setEOLComment(addr(0x02239C82), "num_chars + string ref")

println("KC02 labels applied: %d functions, %d data labels" % (28, 31))
