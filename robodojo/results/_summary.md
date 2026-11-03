# RoboDojo Evaluation Summary

## Overview (by capability dimension)

Progress † marks incomplete evaluation (126 = 42 tasks × 3 seeds). Cells show `mean ±std` across seeds; scores reflect completed cells only. Policies are sorted by overall Avg SR.

| Policy | Progress | Generalization SR (%) | Generalization Score | Precision SR (%) | Precision Score | Long-Horizon SR (%) | Long-Horizon Score | Memory SR (%) | Memory Score | Open SR (%) | Open Score | Avg SR (%) | Avg Score |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Auriga | 100% | 48.11 ±1.63 | 52.18 ±1.71 | 38.50 ±2.21 | 46.32 ±1.85 | 45.17 ±1.76 | 62.05 ±1.41 | 91.78 ±2.22 | 92.19 ±2.29 | 62.92 ±2.18 | 65.23 ±2.20 | 57.29 ±0.59 | 63.59 ±0.51 |

## Generalization: Standard vs Random (25 episodes each)

Each cell shows `mean ±std` across seeds, averaged over the 12 Generalization tasks with completed data for that half. Δ = Random − Standard (mean SR). Sorted by Standard SR.

| Policy | Standard SR (%) | Standard Score | Random SR (%) | Random Score | Δ SR | Δ Score |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Auriga | 51.56 ±2.06 | 55.64 ±1.92 | 44.67 ±2.16 | 48.72 ±2.33 | -6.89 | -6.93 |

### Per-task Standard vs Random

Additional view only: each of the 12 Generalization tasks is listed as the 25-episode standard split and its `_random` sibling. Columns match the per-policy all-task tables. Existing merged 50-episode scores are unchanged.

#### Auriga

| Task | Seed 0 SR (%) | Seed 0 Score | Seed 1 SR (%) | Seed 1 Score | Seed 2 SR (%) | Seed 2 Score | Avg SR (%) | Avg Score |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| <nobr>stack_bowls</nobr> | 80 | 81.80 | 96 | 96.60 | 88 | 89.80 | 88 ±8 | 89.40 ±7.40 |
| <nobr>stack_bowls_random</nobr> | 72 | 75.00 | 88 | 89.80 | 84 | 85.80 | 81 ±8 | 83.53 ±7.40 |
| <nobr>push_T</nobr> | 100 | 100.00 | 92 | 92.00 | 68 | 68.00 | 87 ±16 | 86.67 ±16.00 |
| <nobr>push_T_random</nobr> | 96 | 96.00 | 96 | 96.00 | 64 | 64.00 | 85 ±16 | 85.33 ±16.00 |
| <nobr>pack_objects_into_box</nobr> | 20 | 28.20 | 12 | 27.40 | 4 | 24.80 | 12 ±8 | 26.80 ±1.70 |
| <nobr>pack_objects_into_box_random</nobr> | 16 | 22.80 | 12 | 23.40 | 8 | 25.60 | 12 ±4 | 23.93 ±1.40 |
| <nobr>fold_clothes</nobr> | 68 | 72.80 | 76 | 80.80 | 80 | 84.00 | 75 ±6 | 79.20 ±5.60 |
| <nobr>fold_clothes_random</nobr> | 56 | 61.60 | 64 | 70.40 | 72 | 77.60 | 64 ±8 | 69.87 ±8.00 |
| <nobr>hang_mugs</nobr> | 0 | 19.80 | 4 | 22.80 | 0 | 12.40 | 1 ±2 | 18.33 ±5.20 |
| <nobr>hang_mugs_random</nobr> | 4 | 20.60 | 4 | 19.20 | 12 | 22.40 | 7 ±4 | 20.73 ±1.60 |
| <nobr>sweep_blocks</nobr> | 64 | 64.00 | 60 | 60.00 | 56 | 56.00 | 60 ±4 | 60.00 ±4.00 |
| <nobr>sweep_blocks_random</nobr> | 24 | 24.00 | 24 | 24.00 | 16 | 16.00 | 21 ±4 | 21.33 ±4.00 |
| <nobr>pour_liquid_into_cup</nobr> | 8 | 8.00 | 4 | 4.00 | 4 | 4.00 | 5 ±2 | 5.33 ±2.00 |
| <nobr>pour_liquid_into_cup_random</nobr> | 12 | 12.00 | 8 | 8.00 | 8 | 8.00 | 9 ±2 | 9.33 ±2.00 |
| <nobr>make_toast</nobr> | 32 | 37.00 | 28 | 32.00 | 28 | 28.00 | 29 ±2 | 32.33 ±4.50 |
| <nobr>make_toast_random</nobr> | 20 | 21.00 | 32 | 36.00 | 16 | 18.00 | 23 ±8 | 25.00 ±9.00 |
| <nobr>arrange_largest_number</nobr> | 100 | 100.00 | 92 | 95.20 | 88 | 91.40 | 93 ±6 | 95.53 ±4.30 |
| <nobr>arrange_largest_number_random</nobr> | 76 | 81.40 | 88 | 90.80 | 84 | 86.60 | 83 ±6 | 86.27 ±4.70 |
| <nobr>sort_nesting_dolls_by_size</nobr> | 68 | 68.00 | 68 | 68.00 | 68 | 68.00 | 68 ±0 | 68.00 ±0.00 |
| <nobr>sort_nesting_dolls_by_size_random</nobr> | 44 | 44.00 | 60 | 60.00 | 60 | 60.00 | 55 ±8 | 54.67 ±8.00 |
| <nobr>store_laptop_and_headphones</nobr> | 0 | 4.00 | 0 | 5.60 | 0 | 8.80 | 0 ±0 | 6.13 ±2.40 |
| <nobr>store_laptop_and_headphones_random</nobr> | 0 | 5.60 | 0 | 8.80 | 0 | 9.60 | 0 ±0 | 8.00 ±2.00 |
| <nobr>stack_blocks</nobr> | 100 | 100.00 | 100 | 100.00 | 100 | 100.00 | 100 ±0 | 100.00 ±0.00 |
| <nobr>stack_blocks_random</nobr> | 92 | 93.20 | 96 | 96.60 | 100 | 100.00 | 96 ±4 | 96.60 ±3.40 |
| **Avg Standard** | 53.3 | 56.97 | 52.7 | 57.03 | 48.7 | 52.93 | 52 ±2 | 55.64 ±2.05 |
| **Avg Random** | 42.7 | 46.43 | 47.7 | 51.92 | 43.7 | 47.80 | 45 ±2 | 48.72 ±2.74 |

## Auriga

| Task | Seed 0 SR (%) | Seed 0 Score | Seed 1 SR (%) | Seed 1 Score | Seed 2 SR (%) | Seed 2 Score | Avg SR (%) | Avg Score |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| <nobr>align_blocks</nobr> | 98 | 98.00 | 90 | 90.00 | 84 | 84.00 | 91 ±7 | 90.67 ±7.00 |
| <nobr>arrange_largest_number</nobr> | 88 | 90.70 | 90 | 93.00 | 86 | 89.00 | 88 ±2 | 90.90 ±2.00 |
| <nobr>build_tower</nobr> | 24 | 34.40 | 30 | 39.60 | 20 | 31.60 | 25 ±5 | 35.20 ±4.00 |
| <nobr>classify_objects</nobr> | 92 | 94.70 | 100 | 100.00 | 98 | 98.30 | 97 ±4 | 97.67 ±2.65 |
| <nobr>classify_objects_by_language</nobr> | 92 | 94.00 | 88 | 92.80 | 86 | 89.60 | 89 ±3 | 92.13 ±2.20 |
| <nobr>cover_blocks</nobr> | 76 | 77.50 | 88 | 88.70 | 86 | 87.50 | 83 ±6 | 84.57 ±5.60 |
| <nobr>deposit_coin</nobr> | 36 | 44.40 | 22 | 34.40 | 38 | 48.80 | 32 ±8 | 42.53 ±7.20 |
| <nobr>fasten_screws</nobr> | 74 | 83.20 | 70 | 81.20 | 64 | 77.60 | 69 ±5 | 80.67 ±2.80 |
| <nobr>fill_egg_holder</nobr> | 2 | 12.20 | 0 | 10.40 | 0 | 10.40 | 1 ±1 | 11.00 ±0.90 |
| <nobr>fill_pen_holder</nobr> | 8 | 38.90 | 18 | 45.40 | 20 | 44.60 | 15 ±6 | 42.97 ±3.25 |
| <nobr>fold_clothes</nobr> | 62 | 67.20 | 70 | 75.60 | 76 | 80.80 | 69 ±7 | 74.53 ±6.80 |
| <nobr>general_pickup</nobr> | 92 | 92.00 | 94 | 94.00 | 94 | 94.00 | 93 ±1 | 93.33 ±1.00 |
| <nobr>hang_mugs</nobr> | 2 | 20.20 | 4 | 21.00 | 6 | 17.40 | 4 ±2 | 19.53 ±1.80 |
| <nobr>imitate_sorting_sequence</nobr> | 96 | 96.30 | 90 | 91.50 | 96 | 98.00 | 94 ±3 | 95.27 ±3.25 |
| <nobr>insert_key</nobr> | 0 | 15.00 | 0 | 14.40 | 0 | 15.00 | 0 ±0 | 14.80 ±0.30 |
| <nobr>insert_tubes</nobr> | 54 | 66.00 | 40 | 58.00 | 44 | 60.00 | 46 ±7 | 61.33 ±4.00 |
| <nobr>make_kong</nobr> | 28 | 28.00 | 32 | 32.00 | 22 | 22.00 | 27 ±5 | 27.33 ±5.00 |
| <nobr>make_toast</nobr> | 26 | 29.00 | 30 | 34.00 | 22 | 23.00 | 26 ±4 | 28.67 ±5.50 |
| <nobr>match_and_pick_from_conveyor</nobr> | 96 | 96.00 | 98 | 98.00 | 96 | 96.00 | 97 ±1 | 96.67 ±1.00 |
| <nobr>organize_table</nobr> | 24 | 69.50 | 20 | 68.00 | 14 | 69.50 | 19 ±5 | 69.00 ±0.75 |
| <nobr>pack_objects_into_box</nobr> | 18 | 25.50 | 12 | 25.40 | 6 | 25.20 | 12 ±6 | 25.37 ±0.15 |
| <nobr>pick_from_conveyor_by_image</nobr> | 48 | 48.00 | 48 | 48.00 | 34 | 34.00 | 43 ±7 | 43.33 ±7.00 |
| <nobr>play_Xylophone</nobr> | 82 | 82.00 | 68 | 68.00 | 82 | 82.00 | 77 ±7 | 77.33 ±7.00 |
| <nobr>play_stacking_toy</nobr> | 30 | 57.60 | 42 | 69.80 | 36 | 66.80 | 36 ±6 | 64.73 ±6.10 |
| <nobr>play_tic_tac_toe</nobr> | 80 | 93.00 | 82 | 93.20 | 88 | 97.00 | 83 ±4 | 94.40 ±2.00 |
| <nobr>plug_in_charger</nobr> | 24 | 24.00 | 16 | 16.00 | 22 | 22.00 | 21 ±4 | 20.67 ±4.00 |
| <nobr>pour_balls_into_vase</nobr> | 32 | 32.00 | 38 | 38.00 | 44 | 44.00 | 38 ±6 | 38.00 ±6.00 |
| <nobr>pour_by_language</nobr> | 8 | 8.80 | 4 | 4.00 | 0 | 1.80 | 4 ±4 | 4.87 ±3.50 |
| <nobr>pour_liquid_into_cup</nobr> | 10 | 10.00 | 6 | 6.00 | 6 | 6.00 | 7 ±2 | 7.33 ±2.00 |
| <nobr>press_by_number</nobr> | 92 | 92.00 | 96 | 96.00 | 96 | 96.00 | 95 ±2 | 94.67 ±2.00 |
| <nobr>push_T</nobr> | 98 | 98.00 | 94 | 94.00 | 66 | 66.00 | 86 ±16 | 86.00 ±16.00 |
| <nobr>put_bottles_into_dustbin</nobr> | 82 | 88.90 | 86 | 91.60 | 80 | 87.40 | 83 ±3 | 89.30 ±2.10 |
| <nobr>solve_equation</nobr> | 88 | 88.00 | 86 | 86.00 | 88 | 88.00 | 87 ±1 | 87.33 ±1.00 |
| <nobr>sort_nesting_dolls_by_size</nobr> | 56 | 56.00 | 64 | 64.00 | 64 | 64.00 | 61 ±4 | 61.33 ±4.00 |
| <nobr>stack_blocks</nobr> | 96 | 96.60 | 98 | 98.30 | 100 | 100.00 | 98 ±2 | 98.30 ±1.70 |
| <nobr>stack_blocks_by_language</nobr> | 96 | 96.80 | 98 | 98.40 | 94 | 95.20 | 96 ±2 | 96.80 ±1.60 |
| <nobr>stack_bowls</nobr> | 76 | 78.40 | 92 | 93.20 | 86 | 87.80 | 85 ±8 | 86.47 ±7.40 |
| <nobr>store_laptop_and_headphones</nobr> | 0 | 4.80 | 0 | 7.20 | 0 | 9.20 | 0 ±0 | 7.07 ±2.20 |
| <nobr>store_tools_in_toolbox</nobr> | 0 | 16.50 | 0 | 11.00 | 0 | 12.50 | 0 ±0 | 13.33 ±2.75 |
| <nobr>swap_T</nobr> | 76 | 76.00 | 92 | 92.00 | 88 | 88.00 | 85 ±8 | 85.33 ±8.00 |
| <nobr>swap_blocks</nobr> | 96 | 96.00 | 98 | 98.00 | 96 | 96.00 | 97 ±1 | 96.67 ±1.00 |
| <nobr>sweep_blocks</nobr> | 44 | 44.00 | 42 | 42.00 | 36 | 36.00 | 41 ±4 | 40.67 ±4.00 |
| **Avg (per seed)** | 54.8 | 60.95 | 55.6 | 61.95 | 53.9 | 60.52 | 55 ±1 | 61.14 ±0.72 |

