// Task identifiers follow the RoboDojo task configuration names. Keep the
// original identifier in titles and filter values so saved links still work.
const taskNames = {
  align_blocks: '对齐积木',
  arrange_largest_number: '排列最大数字',
  build_tower: '搭建高塔',
  classify_objects: '物体分类',
  classify_objects_by_language: '按语言指令分类物体',
  cover_blocks: '盖住积木',
  deposit_coin: '投放硬币',
  fasten_screws: '拧紧螺丝',
  fill_egg_holder: '装满蛋托',
  fill_pen_holder: '把笔放入笔筒',
  fold_clothes: '折叠衣物',
  general_pickup: '拾取物体',
  hang_mugs: '悬挂马克杯',
  imitate_sorting_sequence: '模仿排序顺序',
  insert_key: '插入钥匙',
  insert_tubes: '插入管件',
  make_kong: '麻将杠牌',
  make_toast: '制作吐司',
  match_and_pick_from_conveyor: '匹配并拾取传送带物体',
  organize_table: '整理桌面',
  pack_objects_into_box: '把物体装入盒子',
  pick_from_conveyor_by_image: '按图像拾取传送带物体',
  play_stacking_toy: '玩叠叠乐玩具',
  play_tic_tac_toe: '下井字棋',
  play_xylophone: '敲木琴',
  plug_in_charger: '插入充电器',
  pour_balls_into_vase: '把小球倒入花瓶',
  pour_by_language: '按语言指令倾倒',
  pour_liquid_into_cup: '把液体倒入杯子',
  press_by_number: '按数字顺序按压',
  push_t: '推动 T 形物体',
  put_bottles_into_dustbin: '把瓶子放入垃圾桶',
  solve_equation: '解方程',
  sort_nesting_dolls_by_size: '按大小排列套娃',
  stack_blocks: '堆叠积木',
  stack_blocks_by_language: '按语言指令堆叠积木',
  stack_bowls: '堆叠碗',
  store_laptop_and_headphones: '收纳笔记本电脑和耳机',
  store_tools_in_toolbox: '把工具收进工具箱',
  swap_t: '交换 T 形物体',
  swap_blocks: '交换积木',
  sweep_blocks: '清扫积木',
};

export function taskLabel(task) {
  if (typeof task !== 'string' || !task.trim() || task === 'unknown') return '任务未注明';
  const key = task.toLowerCase(), random = key.endsWith('_random');
  const translated = taskNames[random ? key.slice(0, -7) : key];
  return translated ? translated + (random ? '（随机版）' : '') : `未翻译任务：${task}`;
}

export function environmentLabel(record = {}) {
  const manifest = record.manifest || {}, runtime = record.runtime || {}, metadata = record.metadata || {};
  const identifiers = [record.environment, metadata.environment, record.data_origin, metadata.data_origin,
    manifest.data_origin, runtime.capabilities?.data_origin, manifest.capabilities?.data_origin,
    runtime.capabilities?.backend, manifest.capabilities?.backend, record.summary?.runtime_protocol,
    manifest.runtime_protocol].filter((value) => typeof value === 'string' && value);
  if (identifiers.some((value) => /synthetic|fixture/i.test(value))) return '合成测试环境';
  if (identifiers.some((value) => /robodojo/i.test(value))) return 'RoboDojo 仿真环境';
  if (identifiers.some((value) => /roboprobe/i.test(value))) return 'RoboProbe 仿真环境';
  return identifiers[0] || '环境未注明';
}
